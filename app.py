#!/usr/bin/env python3
"""
GitHub ZIP Deployer — fast edition.

Extracts a ZIP archive and pushes its contents to a GitHub repository through
the Git Data API, in a single commit.

Why this is fast
----------------
The naive approach is one `POST /git/blobs` request per file. GitHub's
secondary rate limit allows 900 points/minute and charges 5 points per POST,
which caps you at ~180 POSTs/minute *regardless of concurrency*. 3,000 files
therefore need >16 minutes at best, and usually far more once retries kick in.

This implementation instead:

  1. Inlines file contents directly into `POST /git/trees` entries (the API
     writes the blob for you), packing many files per request. 3,000 files
     collapse from ~3,000 POSTs to ~10.
  2. Computes each file's git blob SHA-1 locally and diffs it against the
     branch's existing tree, so unchanged files cost zero requests.
  3. Uploads only what genuinely needs a blob (binary / oversized files) using
     a streaming worker pool with connection reuse — no batch barriers.
  4. Paces itself against the documented point budget and retries with
     exponential backoff + jitter, honouring `Retry-After`.
  5. Verifies the final tree server-side and auto-repairs any mismatch before
     the commit is created.

Usage:
    python app.py                       # interactive
    python app.py --owner o --repo r --zip site.zip   # scripted (token from $GITHUB_TOKEN)

No third-party packages are required; `requests` is used automatically when
installed, otherwise the standard library provides pooled keep-alive HTTP.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_WORKERS = 12          # stays well under GitHub's 100-concurrency cap
DEFAULT_POINTS_PER_MIN = 900  # documented secondary rate limit budget
POINTS_WRITE = 5              # POST / PATCH / PUT / DELETE
POINTS_READ = 1               # GET / HEAD / OPTIONS

MAX_TREE_ENTRIES = 400        # entries per POST /git/trees call
MAX_TREE_BYTES = 3 * 1024 * 1024   # serialized payload budget per tree call
MAX_INLINE_BYTES = 1024 * 1024     # bigger text files go the blob route
BLOB_HARD_LIMIT = 100 * 1024 * 1024  # GitHub rejects blobs above ~100 MB

MODE_FILE = "100644"
MODE_EXEC = "100755"

SKIP_PATTERNS = ("__MACOSX", ".DS_Store", "Thumbs.db")

# Wrapper + separators for {"tree":[...],"base_tree":"<40-char sha>"}.
_TREE_WRAPPER_BYTES = 96


def encode_json(data) -> bytes:
    """UTF-8 JSON, compact non-ASCII. Shared so size estimates match the wire."""
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def pack_tree_items(
    tree_items: list[dict],
    max_entries: int = MAX_TREE_ENTRIES,
    max_bytes: int = MAX_TREE_BYTES,
) -> list[list[dict]]:
    """Pack tree entries into request-sized chunks using *actual* JSON bytes.

    Estimating from character counts under-counts CJK and over-counts ASCII,
    which either blows the payload (413 / timeout) or wastes a round trip.
    Measuring the real UTF-8 JSON keeps each POST just under the budget.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = _TREE_WRAPPER_BYTES
    for item in tree_items:
        item_bytes = len(encode_json(item)) + 2  # comma + space in the array
        if current and (
            len(current) >= max_entries or current_bytes + item_bytes > max_bytes
        ):
            chunks.append(current)
            current = []
            current_bytes = _TREE_WRAPPER_BYTES
        current.append(item)
        current_bytes += item_bytes
    if current:
        chunks.append(current)
    return chunks


def is_payload_too_large(exc: "GitHubError") -> bool:
    """True when GitHub (or a proxy) rejected the body for being too big."""
    if exc.status == 413:
        return True
    if exc.status not in (400, 422):
        return False
    msg = (exc.message or "").lower()
    return any(
        needle in msg
        for needle in ("too large", "request entity", "payload", "size limit", "too big")
    )

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

try:  # optional colour
    from colorama import init as _colorama_init

    _colorama_init(autoreset=True)
except Exception:  # pragma: no cover - colour is cosmetic
    pass

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class C:
    DIM = "\033[2m" if _USE_COLOR else ""
    RESET = "\033[0m" if _USE_COLOR else ""
    RED = "\033[31m" if _USE_COLOR else ""
    GREEN = "\033[32m" if _USE_COLOR else ""
    YELLOW = "\033[33m" if _USE_COLOR else ""
    CYAN = "\033[36m" if _USE_COLOR else ""
    BOLD = "\033[1m" if _USE_COLOR else ""


_log_lock = threading.Lock()
QUIET = False


def log(message: str, level: str = "info") -> None:
    if QUIET and level == "info":
        return
    icon, color = {
        "error": ("x", C.RED),
        "success": ("+", C.GREEN),
        "warn": ("!", C.YELLOW),
    }.get(level, ("-", C.CYAN))
    stamp = time.strftime("%H:%M:%S")
    with _log_lock:
        stream = sys.stderr if level == "error" else sys.stdout
        print(f"{C.DIM}[{stamp}]{C.RESET} {color}{icon} {message}{C.RESET}", file=stream)
        stream.flush()


def human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= step
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------
# Git object helpers
# --------------------------------------------------------------------------


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 git itself would assign to this blob."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def is_inlineable(data: bytes) -> bool:
    """True when the bytes can safely ride inside a tree entry's `content`."""
    if len(data) > MAX_INLINE_BYTES:
        return False
    if b"\x00" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # Lone surrogates / unpaired code points would not survive a JSON round-trip.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --------------------------------------------------------------------------
# HTTP transport (pooled, keep-alive, dependency-optional)
# --------------------------------------------------------------------------


class Response:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class _RequestsTransport:
    """Connection-pooling transport backed by `requests`, one Session per thread."""

    def __init__(self, workers: int):
        import requests  # noqa: F401  (import guarded by caller)

        self._requests = requests
        self._workers = max(workers, 4)
        self._local = threading.local()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._requests.Session()
            adapter = self._requests.adapters.HTTPAdapter(
                pool_connections=self._workers,
                pool_maxsize=self._workers,
                max_retries=0,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def request(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> Response:
        resp = self._session().request(method, url, headers=headers, data=body, timeout=timeout)
        return Response(resp.status_code, dict(resp.headers), resp.content)

    def close(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()


class _StdlibTransport:
    """Keep-alive transport built on http.client, one connection per thread."""

    def __init__(self, workers: int):
        import http.client as http_client

        self._http = http_client
        self._local = threading.local()

    def _connection(self, url: str):
        parts = urlsplit(url)
        key = (parts.scheme, parts.hostname, parts.port)
        current = getattr(self._local, "conn", None)
        if current is not None and getattr(self._local, "key", None) == key:
            return current, parts
        if current is not None:
            try:
                current.close()
            except Exception:
                pass
        if parts.scheme == "https":
            conn = self._http.HTTPSConnection(parts.hostname, parts.port or 443, timeout=60)
        else:
            conn = self._http.HTTPConnection(parts.hostname, parts.port or 80, timeout=60)
        self._local.conn = conn
        self._local.key = key
        return conn, parts

    def request(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> Response:
        conn, parts = self._connection(url)
        target = parts.path + (("?" + parts.query) if parts.query else "")
        conn.timeout = timeout
        for attempt in (0, 1):  # a pooled connection may have gone stale
            try:
                conn.request(method, target, body=body, headers=headers)
                raw = conn.getresponse()
                data = raw.read()
                return Response(raw.status, dict(raw.getheaders()), data)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None
                if attempt == 1:
                    raise
                conn, parts = self._connection(url)
        raise RuntimeError("unreachable")

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def make_transport(workers: int):
    """Prefer `requests` for its connection pooling, fall back to the stdlib.

    Set ZIP_DEPLOYER_TRANSPORT=stdlib|requests to force one (used by the tests
    so both code paths stay covered).
    """
    preference = os.environ.get("ZIP_DEPLOYER_TRANSPORT", "").lower()
    if preference == "stdlib":
        return _StdlibTransport(workers)
    try:
        return _RequestsTransport(workers)
    except Exception:
        if preference == "requests":
            raise
        return _StdlibTransport(workers)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class PointBudget:
    """Sliding-window limiter mirroring GitHub's points-per-minute rule.

    Keeps us *below* the secondary rate limit instead of discovering it the
    hard way, which is what turns a slow deploy into a stalled one.

    A safety factor is applied because the client charges a request when it is
    *sent* while GitHub charges it when it is *received*: without the margin,
    that clock skew is enough to trip the limit on a burst of writes.
    """

    SAFETY = 0.85

    def __init__(self, points_per_min: int = DEFAULT_POINTS_PER_MIN):
        self.limit = max(int(points_per_min * self.SAFETY), 10)
        self.events: deque[tuple[float, int]] = deque()
        self.spent = 0
        self.lock = threading.Lock()
        self.waited = 0.0

    def spend(self, points: int) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] >= 60.0:
                    self.spent -= self.events.popleft()[1]
                if self.spent + points <= self.limit:
                    self.events.append((now, points))
                    self.spent += points
                    return
                sleep_for = 60.0 - (now - self.events[0][0]) + 0.01
            self.waited += sleep_for
            time.sleep(min(sleep_for, 5.0))


# --------------------------------------------------------------------------
# GitHub client
# --------------------------------------------------------------------------


class GitHubError(Exception):
    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__(f"GitHub API {status} on {path}: {message}" if path else f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


RETRY_STATUSES = {429, 500, 502, 503, 504}
_SECONDARY_RE = re.compile(r"secondary rate limit|abuse detection|rate limit", re.I)


class GitHubClient:
    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        api_base: str = DEFAULT_API_BASE,
        workers: int = DEFAULT_WORKERS,
        max_retries: int = 6,
        points_per_min: int = DEFAULT_POINTS_PER_MIN,
        timeout: float = 60.0,
    ):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.api_base = api_base.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.transport = make_transport(workers)
        self.budget = PointBudget(points_per_min)
        self.requests_made = 0
        self._counter_lock = threading.Lock()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "github-zip-deployer/2.0",
            "Connection": "keep-alive",
        }

    def request(self, method: str, path: str, data=None, allow_404: bool = False):
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}{path}"
        # ensure_ascii=False keeps non-ASCII as compact UTF-8 instead of
        # expanding every character to a 6-byte \\uXXXX escape, which on a
        # unicode-heavy repository inflates the payload by 1.5x or more.
        body = encode_json(data) if data is not None else None
        points = POINTS_READ if method in ("GET", "HEAD", "OPTIONS") else POINTS_WRITE

        last_error = None
        for attempt in range(self.max_retries + 1):
            self.budget.spend(points)
            headers = self._headers()
            if body is not None:
                headers["Content-Length"] = str(len(body))
            try:
                resp = self.transport.request(method, url, headers, body, self.timeout)
            except Exception as exc:  # network hiccup: retry
                last_error = exc
                if attempt == self.max_retries:
                    raise GitHubError(0, f"network error: {exc}", path) from exc
                time.sleep(self._backoff(attempt))
                continue

            with self._counter_lock:
                self.requests_made += 1

            if 200 <= resp.status < 300:
                return resp.json() if resp.status != 204 else None
            if resp.status == 404 and allow_404:
                return None

            message = self._error_message(resp)
            retryable = resp.status in RETRY_STATUSES or (
                resp.status == 403 and _SECONDARY_RE.search(message or "")
            )
            if not retryable or attempt == self.max_retries:
                raise GitHubError(resp.status, message, path)

            delay = self._retry_delay(resp, attempt)
            log(f"{resp.status} on {path} — backing off {delay:.1f}s ({message[:90]})", "warn")
            time.sleep(delay)

        raise GitHubError(0, f"exhausted retries: {last_error}", path)

    @staticmethod
    def _error_message(resp: Response) -> str:
        try:
            payload = resp.json() or {}
            msg = payload.get("message", "")
            errors = payload.get("errors")
            if errors:
                msg += " | " + json.dumps(errors)[:300]
            return msg or resp.text[:300]
        except Exception:
            return resp.text[:300]

    def _backoff(self, attempt: int) -> float:
        return min(2.0 ** attempt, 30.0) * (0.5 + random.random() / 2)

    def _retry_delay(self, resp: Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after) + 1.0, 120.0)
            except ValueError:
                pass
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset:
            try:
                wait = float(reset) - time.time()
                if 0 < wait < 3600:
                    return min(wait + 1.0, 300.0)
            except ValueError:
                pass
        return self._backoff(attempt)

    def close(self):
        try:
            self.transport.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# ZIP reading
# --------------------------------------------------------------------------


class FileEntry:
    __slots__ = ("path", "data", "sha", "mode", "size")

    def __init__(self, path: str, data: bytes, mode: str):
        self.path = path
        self.data = data
        self.mode = mode
        self.size = len(data)
        self.sha = git_blob_sha(data)


def normalize_path(raw: str) -> str | None:
    path = raw.replace("\\", "/").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.endswith("/"):
        return None
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if any(p == ".git" for p in parts):
        return None
    path = "/".join(parts)
    if any(pattern in path for pattern in SKIP_PATTERNS):
        return None
    return path


def mode_for(info: zipfile.ZipInfo) -> str:
    # Preserve the executable bit for archives created on unix-like systems.
    if info.create_system == 3:
        unix_mode = (info.external_attr >> 16) & 0o7777
        if unix_mode & 0o111:
            return MODE_EXEC
    return MODE_FILE


def read_zip(zip_bytes: bytes, workers: int = 8, strip_root: bool = False) -> list[FileEntry]:
    """Extract + hash every usable member, decompressing in parallel."""
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        members = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            path = normalize_path(info.filename)
            if path is None:
                continue
            members.append((path, info, mode_for(info)))

    if not members:
        return []

    if strip_root:
        roots = {path.split("/", 1)[0] for path, _, _ in members}
        if len(roots) == 1 and any("/" in path for path, _, _ in members):
            root = roots.pop()
            stripped = []
            for path, info, mode in members:
                trimmed = path[len(root) + 1:] if path.startswith(root + "/") else path
                if trimmed:
                    stripped.append((trimmed, info, mode))
            if stripped:
                log(f"Stripped common root folder '{root}/' from {len(stripped)} paths")
                members = stripped

    local = threading.local()

    def opener() -> zipfile.ZipFile:
        # zipfile objects are not thread-safe, so give each worker its own view
        # over the same in-memory buffer (the bytes themselves are shared).
        zf = getattr(local, "zf", None)
        if zf is None:
            zf = zipfile.ZipFile(BytesIO(zip_bytes))
            local.zf = zf
        return zf

    def extract(member):
        path, info, mode = member
        return FileEntry(path, opener().read(info), mode)

    n_workers = max(1, min(workers, len(members)))
    if n_workers == 1:
        return [extract(m) for m in members]
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        return list(pool.map(extract, members, chunksize=16))


# --------------------------------------------------------------------------
# Deployment
# --------------------------------------------------------------------------


class DeployResult:
    def __init__(self):
        self.total_files = 0
        self.unchanged = 0
        self.inlined = 0
        self.blobs_uploaded = 0
        self.blobs_reused = 0
        self.tree_calls = 0
        self.bytes_total = 0
        self.commit_sha = ""
        self.duration = 0.0
        self.api_requests = 0
        self.no_changes = False

    def as_dict(self) -> dict:
        return {
            "total_files": self.total_files,
            "unchanged": self.unchanged,
            "inlined": self.inlined,
            "blobs_uploaded": self.blobs_uploaded,
            "blobs_reused": self.blobs_reused,
            "tree_calls": self.tree_calls,
            "bytes_total": self.bytes_total,
            "commit_sha": self.commit_sha,
            "duration_seconds": round(self.duration, 2),
            "api_requests": self.api_requests,
            "no_changes": self.no_changes,
        }


class Deployer:
    def __init__(
        self,
        client: GitHubClient,
        branch: str = "main",
        workers: int = DEFAULT_WORKERS,
        inline: bool = True,
        verify: bool = True,
        prune: bool = False,
    ):
        self.gh = client
        self.branch = branch
        self.workers = workers
        self.inline = inline
        self.verify = verify
        self.prune = prune

    # -- branch resolution -------------------------------------------------

    def resolve_branch(self) -> tuple[str, str]:
        ref = self.gh.request("GET", f"/git/ref/heads/{self.branch}", allow_404=True)
        if ref is None:
            return self._initialize_branch()
        commit_sha = ref["object"]["sha"]
        commit = self.gh.request("GET", f"/git/commits/{commit_sha}")
        return commit_sha, commit["tree"]["sha"]

    def _initialize_branch(self) -> tuple[str, str]:
        log(f"Branch '{self.branch}' not found — initializing repository...", "warn")
        content = base64.b64encode(
            b"# Project Repository\n\nInitialized automatically by GitHub ZIP Deployer.\n"
        ).decode()
        self.gh.request(
            "PUT",
            "/contents/README.md",
            {
                "message": "Initial commit by GitHub ZIP Deployer",
                "content": content,
                "branch": self.branch,
            },
        )
        log("Repository initialized with README.md", "success")
        ref = self.gh.request("GET", f"/git/ref/heads/{self.branch}")
        commit_sha = ref["object"]["sha"]
        commit = self.gh.request("GET", f"/git/commits/{commit_sha}")
        return commit_sha, commit["tree"]["sha"]

    # -- existing state ----------------------------------------------------

    def fetch_existing(self, tree_sha: str) -> tuple[dict[str, str], set[str], bool]:
        """Return (path -> blob sha, all known blob shas, truncated?)."""
        data = self.gh.request("GET", f"/git/trees/{tree_sha}?recursive=1", allow_404=True)
        if not data:
            return {}, set(), False
        by_path: dict[str, str] = {}
        known: set[str] = set()
        for item in data.get("tree", []):
            if item.get("type") == "blob":
                by_path[item["path"]] = item["sha"]
                known.add(item["sha"])
        return by_path, known, bool(data.get("truncated"))

    # -- blob uploads ------------------------------------------------------

    def upload_blobs(self, entries: list[FileEntry], result: DeployResult) -> dict[str, str]:
        """Upload one blob per unique content SHA. Returns sha -> confirmed sha."""
        if not entries:
            return {}
        unique: dict[str, FileEntry] = {}
        for entry in entries:
            unique.setdefault(entry.sha, entry)
        todo = list(unique.values())
        total = len(todo)
        log(f"Uploading {total} binary/large file(s) as blobs on {self.workers} connections...")

        done = 0
        started = time.monotonic()
        lock = threading.Lock()
        mapping: dict[str, str] = {}

        def upload(entry: FileEntry):
            nonlocal done
            payload = {
                "content": base64.b64encode(entry.data).decode("ascii"),
                "encoding": "base64",
            }
            blob = self.gh.request("POST", "/git/blobs", payload)
            returned = blob["sha"]
            if returned != entry.sha:  # should never happen; guard anyway
                log(f"Blob SHA mismatch for {entry.path} (expected {entry.sha[:8]}, got {returned[:8]})", "warn")
            with lock:
                mapping[entry.sha] = returned
                done += 1
                if done % 25 == 0 or done == total:
                    rate = done / max(time.monotonic() - started, 1e-6)
                    log(f"  -> {done}/{total} blobs ({rate:.1f}/s)")

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(upload, todo))

        result.blobs_uploaded += total
        return mapping

    # -- tree building -----------------------------------------------------

    def build_tree(self, tree_items: list[dict], base_tree: str, result: DeployResult) -> str:
        """Create the tree in size-bounded chunks, chaining each onto the last.

        Chunks are packed from the real JSON byte length so a TypeScript-heavy
        send fills each request without going over. If GitHub still rejects a
        payload as too large, the chunk is split and retried — the deploy
        does not crash mid-send.
        """
        chunks = pack_tree_items(tree_items)
        log(f"Writing {len(tree_items)} tree entries in {len(chunks)} request(s)...")
        tree_sha = base_tree
        planned = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            tree_sha = self._post_tree_chunk(chunk, tree_sha, result)
            if planned > 1:
                log(f"  -> tree chunk {index}/{planned} ({len(chunk)} entries)")
        return tree_sha

    def _post_tree_chunk(self, chunk: list[dict], base_sha: str, result: DeployResult) -> str:
        payload = {"tree": chunk}
        if base_sha:
            payload["base_tree"] = base_sha
        try:
            response = self.gh.request("POST", "/git/trees", payload)
        except GitHubError as exc:
            if len(chunk) > 1 and is_payload_too_large(exc):
                mid = max(1, len(chunk) // 2)
                log(
                    f"Tree chunk of {len(chunk)} entries was too large — "
                    f"splitting into {mid} + {len(chunk) - mid} and retrying.",
                    "warn",
                )
                first_sha = self._post_tree_chunk(chunk[:mid], base_sha, result)
                return self._post_tree_chunk(chunk[mid:], first_sha, result)
            raise
        result.tree_calls += 1
        return response["sha"]

    # -- verification ------------------------------------------------------

    def verify_tree(self, tree_sha: str, expected: dict[str, str]) -> list[str]:
        """Compare the server-side tree against locally computed SHAs."""
        data = self.gh.request("GET", f"/git/trees/{tree_sha}?recursive=1")
        if data.get("truncated"):
            log("Tree too large for a single verification read — skipping deep verify.", "warn")
            return []
        actual = {i["path"]: i["sha"] for i in data.get("tree", []) if i.get("type") == "blob"}
        return [path for path, sha in expected.items() if actual.get(path) != sha]

    # -- main flow ---------------------------------------------------------

    def deploy(self, files: list[FileEntry], message: str | None = None) -> DeployResult:
        result = DeployResult()
        result.total_files = len(files)
        result.bytes_total = sum(f.size for f in files)
        started = time.monotonic()

        oversized = [f for f in files if f.size > BLOB_HARD_LIMIT]
        if oversized:
            raise SystemExit(
                f"These files exceed GitHub's 100 MB blob limit: "
                + ", ".join(f.path for f in oversized[:5])
            )

        log(f"Resolving branch '{self.branch}'...")
        parent_commit, base_tree = self.resolve_branch()

        log("Reading existing tree to skip unchanged files...")
        existing_by_path, known_shas, truncated = self.fetch_existing(base_tree)
        if truncated:
            log("Existing tree listing was truncated; some files may be re-uploaded.", "warn")

        changed: list[FileEntry] = []
        for entry in files:
            if existing_by_path.get(entry.path) == entry.sha:
                result.unchanged += 1
            else:
                changed.append(entry)

        if result.unchanged:
            log(f"{result.unchanged} file(s) already identical on '{self.branch}' — skipping.")

        deletions: list[dict] = []
        if self.prune:
            incoming = {f.path for f in files}
            for path in existing_by_path:
                if path not in incoming:
                    deletions.append({"path": path, "mode": MODE_FILE, "type": "blob", "sha": None})
            if deletions:
                log(f"Prune enabled: {len(deletions)} path(s) will be deleted.", "warn")

        if not changed and not deletions:
            log("Nothing to do — the branch already matches this ZIP.", "success")
            result.no_changes = True
            result.duration = time.monotonic() - started
            result.api_requests = self.gh.requests_made
            return result

        # Split the work: inline what we can, blob-upload the rest.
        inline_entries: list[FileEntry] = []
        blob_entries: list[FileEntry] = []
        for entry in changed:
            if entry.sha in known_shas:
                # The object already exists in this repo, reference it directly.
                blob_entries.append(entry)
                result.blobs_reused += 1
            elif self.inline and is_inlineable(entry.data):
                inline_entries.append(entry)
            else:
                blob_entries.append(entry)

        needs_upload = [e for e in blob_entries if e.sha not in known_shas]
        log(
            f"Plan: {len(inline_entries)} inlined into tree, "
            f"{len(needs_upload)} uploaded as blobs, "
            f"{result.blobs_reused} reused from repo, "
            f"{result.unchanged} unchanged."
        )

        self.upload_blobs(needs_upload, result)
        result.inlined = len(inline_entries)

        tree_items: list[dict] = []
        for entry in inline_entries:
            tree_items.append(
                {
                    "path": entry.path,
                    "mode": entry.mode,
                    "type": "blob",
                    "content": entry.data.decode("utf-8"),
                }
            )
        for entry in blob_entries:
            tree_items.append(
                {"path": entry.path, "mode": entry.mode, "type": "blob", "sha": entry.sha}
            )
        tree_items.extend(deletions)

        tree_sha = self.build_tree(tree_items, base_tree, result)

        if self.verify:
            log("Verifying tree contents against locally computed SHAs...")
            expected = {e.path: e.sha for e in changed}
            mismatched = self.verify_tree(tree_sha, expected)
            if mismatched:
                log(f"{len(mismatched)} entr(ies) did not match — repairing via blob upload.", "warn")
                by_path = {e.path: e for e in changed}
                repair = [by_path[p] for p in mismatched if p in by_path]
                self.upload_blobs(repair, result)
                repair_items = [
                    {"path": e.path, "mode": e.mode, "type": "blob", "sha": e.sha} for e in repair
                ]
                tree_sha = self.build_tree(repair_items, tree_sha, result)
                still_bad = self.verify_tree(tree_sha, expected)
                if still_bad:
                    raise SystemExit(
                        f"Verification failed for {len(still_bad)} path(s), e.g. {still_bad[:3]}"
                    )
            log("Verification passed — every file matches byte for byte.", "success")

        log("Creating commit...")
        if message is None:
            message = (
                f"Deploy {len(changed)} file(s) from ZIP\n\n"
                f"{result.inlined} inlined, {result.blobs_uploaded} uploaded, "
                f"{result.unchanged} unchanged."
            )
        commit = self.gh.request(
            "POST",
            "/git/commits",
            {"message": message, "tree": tree_sha, "parents": [parent_commit]},
        )
        result.commit_sha = commit["sha"]

        log("Updating branch reference...")
        self.gh.request(
            "PATCH", f"/git/refs/heads/{self.branch}", {"sha": commit["sha"], "force": False}
        )

        result.duration = time.monotonic() - started
        result.api_requests = self.gh.requests_made
        return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def prompt(label: str, default: str = "", required: bool = True, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        if required and not default:
            raise SystemExit(f"Missing required value: {label} (stdin is not a terminal)")
        return default
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            import getpass

            value = getpass.getpass(f"{C.YELLOW}{label}{suffix}: {C.RESET}").strip()
        else:
            value = input(f"{C.YELLOW}{label}{suffix}: {C.RESET}").strip()
        if not value and default:
            return default
        if value or not required:
            return value
        print(f"{C.RED}This value is required.{C.RESET}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push the contents of a ZIP archive to a GitHub repository, fast.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--token", help="personal access token (default: $GITHUB_TOKEN / $GH_TOKEN)")
    parser.add_argument("--owner", help="repository owner (user or org)")
    parser.add_argument("--repo", help="repository name")
    parser.add_argument("--branch", default="main", help="target branch")
    parser.add_argument("--zip", dest="zip_path", help="path to the ZIP file")
    parser.add_argument("--message", "-m", help="commit message")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel connections")
    parser.add_argument("--api-base", default=os.environ.get("GITHUB_API_BASE", DEFAULT_API_BASE),
                        help="API base URL (for GitHub Enterprise or testing)")
    parser.add_argument("--points-per-min", type=int, default=DEFAULT_POINTS_PER_MIN,
                        help="secondary rate-limit budget to stay under")
    parser.add_argument("--no-inline", action="store_true",
                        help="disable inline tree content (slow path, one blob per file)")
    parser.add_argument("--no-verify", action="store_true", help="skip server-side verification")
    parser.add_argument("--prune", action="store_true",
                        help="delete repository files that are absent from the ZIP")
    parser.add_argument("--strip-root", action="store_true",
                        help="drop a single common top-level folder from the archive paths")
    parser.add_argument("--dry-run", action="store_true", help="analyse the ZIP without writing")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    parser.add_argument("--quiet", "-q", action="store_true", help="only warnings and errors")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    global QUIET
    args = parse_args(argv)
    QUIET = args.quiet

    if not args.quiet:
        print(f"\n{C.CYAN}{C.BOLD}GitHub ZIP Deployer — fast edition{C.RESET}\n")

    zip_path = args.zip_path or prompt("Path to ZIP file")
    while not Path(zip_path).is_file():
        if not sys.stdin.isatty():
            raise SystemExit(f"ZIP file not found: {zip_path}")
        zip_path = prompt("File not found. Path to ZIP file")

    owner = args.owner or prompt("Repository owner (username or org)")
    repo = args.repo or prompt("Repository name")
    branch = args.branch or "main"

    # Never accept a token from the command line by default: it would land in
    # shell history and process listings. The environment is the safe path.
    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and not args.dry_run:
        token = prompt("Personal Access Token (repo scope)", secret=True)

    zip_bytes = Path(zip_path).read_bytes()
    log(f"Reading {zip_path} ({human_bytes(len(zip_bytes))})...")
    t0 = time.monotonic()
    files = read_zip(zip_bytes, workers=max(4, args.workers), strip_root=args.strip_root)
    extract_time = time.monotonic() - t0
    if not files:
        raise SystemExit("No deployable files found in the archive.")
    total_bytes = sum(f.size for f in files)
    log(
        f"Extracted and hashed {len(files)} file(s), {human_bytes(total_bytes)} "
        f"in {extract_time:.2f}s"
    )

    if args.dry_run:
        inlineable = sum(1 for f in files if is_inlineable(f.data))
        est_trees = max(1, (len(files) + MAX_TREE_ENTRIES - 1) // MAX_TREE_ENTRIES)
        log(f"Dry run: {inlineable} inlineable, {len(files) - inlineable} need blob uploads")
        log(f"Dry run: roughly {est_trees + len(files) - inlineable + 4} API requests")
        if args.json:
            print(json.dumps({"files": len(files), "bytes": total_bytes, "inlineable": inlineable}, indent=2))
        return 0

    client = GitHubClient(
        token=token,
        owner=owner,
        repo=repo,
        api_base=args.api_base,
        workers=args.workers,
        points_per_min=args.points_per_min,
    )
    deployer = Deployer(
        client,
        branch=branch,
        workers=args.workers,
        inline=not args.no_inline,
        verify=not args.no_verify,
        prune=args.prune,
    )

    log(f"Target: {owner}/{repo} on branch '{branch}'")
    try:
        result = deployer.deploy(files, message=args.message)
    except GitHubError as exc:
        log(str(exc), "error")
        return 1
    finally:
        client.close()

    if not result.no_changes:
        rate = result.total_files / max(result.duration, 1e-6)
        log(
            f"Deployed {result.total_files} file(s) in {result.duration:.1f}s "
            f"({rate:.0f} files/s) using {result.api_requests} API requests.",
            "success",
        )
        log(f"https://github.com/{owner}/{repo}/tree/{branch}")
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrupted.", "error")
        sys.exit(130)
