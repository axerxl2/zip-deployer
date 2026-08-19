#!/usr/bin/env python3
"""
A local stand-in for GitHub's Git Data API, used to test and benchmark the
deployer without touching a real repository (or a real access token).

It is deliberately strict, so that passing against it means something:

* Blob SHAs are **real git SHA-1s** (`sha1("blob <len>\\0" + bytes)`), so any
  corruption of file content — encoding, newline, truncation — changes the SHA
  and fails verification.
* Tree entries must supply exactly one of `sha` or `content`, and a `sha` that
  was never written is rejected with 422, just like the real API.
* The documented secondary rate limit is enforced: 900 points per minute,
  5 points per write, 1 per read, replying 403 + Retry-After when exceeded.
* Per-request latency is simulated, including a per-entry cost for large tree
  writes, so a request that carries 400 files is not pretended to be free.

Run standalone:
    python tests/mock_github.py --port 8080 --latency 0.12
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


class Repo:
    """In-memory git object store with flat tree representation."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, tuple[str, str]]] = {}  # sha -> path -> (mode, blob sha)
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.lock = threading.Lock()

    def put_blob(self, data: bytes) -> str:
        sha = git_blob_sha(data)
        self.blobs[sha] = data
        return sha

    def put_tree(self, entries: dict[str, tuple[str, str]]) -> str:
        payload = json.dumps(sorted((p, m, s) for p, (m, s) in entries.items())).encode()
        sha = hashlib.sha1(b"tree" + payload).hexdigest()
        self.trees[sha] = dict(entries)
        return sha

    def files(self, tree_sha: str) -> dict[str, bytes]:
        return {p: self.blobs[s] for p, (_, s) in self.trees.get(tree_sha, {}).items()}


class RateLimiter:
    """GitHub's documented points-per-minute secondary rate limit."""

    def __init__(self, points_per_min: int = 900):
        self.limit = points_per_min
        self.events: deque[tuple[float, int]] = deque()
        self.spent = 0
        self.lock = threading.Lock()
        self.rejections = 0

    def charge(self, points: int) -> float:
        """Return 0.0 if allowed, else the number of seconds to wait."""
        if self.limit <= 0:
            return 0.0
        with self.lock:
            now = time.monotonic()
            while self.events and now - self.events[0][0] >= 60.0:
                self.spent -= self.events.popleft()[1]
            if self.spent + points <= self.limit:
                self.events.append((now, points))
                self.spent += points
                return 0.0
            self.rejections += 1
            return max(60.0 - (now - self.events[0][0]), 1.0)


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_endpoint: dict[str, int] = {}
        self.total = 0
        self.max_concurrency = 0
        self.current = 0
        # Largest request body seen. Packing many files per request is the
        # whole optimisation, so it needs watching: an oversized payload is
        # exactly how it would break against the real API.
        self.max_request_bytes = 0

    def record_body(self, size: int):
        with self.lock:
            self.max_request_bytes = max(self.max_request_bytes, size)

    def start(self, label: str):
        with self.lock:
            self.total += 1
            self.by_endpoint[label] = self.by_endpoint.get(label, 0) + 1
            self.current += 1
            self.max_concurrency = max(self.max_concurrency, self.current)

    def finish(self):
        with self.lock:
            self.current -= 1


class MockGitHub:
    def __init__(
        self,
        latency: float = 0.12,
        write_latency: float = 0.25,
        per_entry_latency: float = 0.002,
        jitter: float = 0.4,
        points_per_min: int = 900,
        fail_every: int = 0,
        max_body_bytes: int = 0,
    ):
        self.repos: dict[str, Repo] = {}
        self.latency = latency
        self.write_latency = write_latency
        self.per_entry_latency = per_entry_latency
        self.jitter = jitter
        self.limiter = RateLimiter(points_per_min)
        self.stats = Stats()
        self.lock = threading.Lock()
        # Fault injection: every Nth request gets a transient 500. Counter
        # based rather than random so tests are reproducible.
        self.fail_every = fail_every
        self.injected_failures = 0
        self._request_counter = 0
        # 0 = unlimited. Set to exercise the deployer's split-and-retry path
        # when a tree POST comes in oversized (real GitHub answers 413).
        self.max_body_bytes = max_body_bytes

    def should_fail(self) -> bool:
        if not self.fail_every:
            return False
        with self.lock:
            self._request_counter += 1
            if self._request_counter % self.fail_every == 0:
                self.injected_failures += 1
                return True
        return False

    def repo(self, owner: str, name: str) -> Repo:
        key = f"{owner}/{name}"
        with self.lock:
            if key not in self.repos:
                self.repos[key] = Repo()
            return self.repos[key]

    def sleep(self, base: float, extra: float = 0.0):
        delay = base * (1.0 - self.jitter / 2 + random.random() * self.jitter) + extra
        time.sleep(max(delay, 0.0))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockGitHub/1.0"
    backend: MockGitHub = None  # injected

    def log_message(self, *args):  # silence
        pass

    # -- plumbing ----------------------------------------------------------

    def _body(self) -> dict:
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _read_body(self) -> None:
        """Always drain the request body.

        On a keep-alive connection an unread body is left in the socket and
        gets parsed as the *next* request, producing spurious 400s. Every code
        path below may return early, so the body is read once up front.
        """
        length = int(self.headers.get("Content-Length") or 0)
        self._raw_body = self.rfile.read(length) if length else b""
        if length:
            self.backend.stats.record_body(length)

    def _send(self, status: int, payload, extra_headers: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _error(self, status: int, message: str, extra_headers=None):
        self._send(status, {"message": message}, extra_headers)

    def _dispatch(self, method: str):
        self._read_body()
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        backend = self.backend

        # The _debug endpoint is a test helper, not part of GitHub's API: it is
        # exempt from rate limiting and fault injection so that assertions can
        # always read the repository state back.
        is_debug = len(parts) > 3 and parts[3] == "_debug"

        if (
            not is_debug
            and backend.max_body_bytes
            and len(getattr(self, "_raw_body", b"")) > backend.max_body_bytes
        ):
            backend.stats.start(f"{method} TOO_LARGE")
            backend.stats.finish()
            self._error(413, "Request Entity Too Large")
            return

        points = 1 if method == "GET" else 5
        wait = 0.0 if is_debug else backend.limiter.charge(points)
        if wait:
            backend.stats.start(f"{method} RATE_LIMITED")
            backend.stats.finish()
            self._error(
                403,
                "You have exceeded a secondary rate limit. Please wait a few minutes before you try again.",
                {"Retry-After": str(int(wait) + 1), "X-RateLimit-Remaining": "0"},
            )
            return

        if len(parts) < 3 or parts[0] != "repos":
            self._error(404, "Not Found")
            return
        owner, name = parts[1], parts[2]

        if not is_debug and backend.should_fail():
            backend.sleep(backend.latency)
            self._error(500, "Injected transient server error")
            return

        rest = parts[3:]
        repo = backend.repo(owner, name)
        query = parse_qs(parsed.query)

        label = f"{method} /{'/'.join(rest[:2])}" if rest else method
        backend.stats.start(label)
        try:
            self._route(method, repo, rest, query, backend)
        finally:
            backend.stats.finish()

    # -- routes ------------------------------------------------------------

    def _route(self, method, repo: Repo, rest, query, backend):
        # GET /git/ref/heads/{branch}
        if method == "GET" and rest[:2] == ["git", "ref"] and len(rest) >= 4:
            branch = "/".join(rest[3:])
            backend.sleep(backend.latency)
            with repo.lock:
                sha = repo.refs.get(branch)
            if not sha:
                self._error(404, "Not Found")
                return
            self._send(200, {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}})
            return

        # GET /git/commits/{sha}
        if method == "GET" and rest[:2] == ["git", "commits"] and len(rest) == 3:
            backend.sleep(backend.latency)
            with repo.lock:
                commit = repo.commits.get(rest[2])
            if not commit:
                self._error(404, "Not Found")
                return
            self._send(200, commit)
            return

        # GET /git/trees/{sha}
        if method == "GET" and rest[:2] == ["git", "trees"] and len(rest) == 3:
            with repo.lock:
                tree = repo.trees.get(rest[2])
            if tree is None:
                backend.sleep(backend.latency)
                self._error(404, "Not Found")
                return
            recursive = bool(query.get("recursive"))
            backend.sleep(backend.latency, extra=len(tree) * backend.per_entry_latency * 0.25)
            entries = [
                {"path": p, "mode": m, "type": "blob", "sha": s, "size": len(repo.blobs.get(s, b""))}
                for p, (m, s) in sorted(tree.items())
            ]
            if not recursive:
                # Non-recursive: only top level; enough for our purposes.
                entries = [e for e in entries if "/" not in e["path"]]
            self._send(200, {"sha": rest[2], "tree": entries, "truncated": len(entries) > 100000})
            return

        # POST /git/blobs
        if method == "POST" and rest[:2] == ["git", "blobs"]:
            body = self._body()
            encoding = body.get("encoding", "utf-8")
            content = body.get("content", "")
            try:
                data = base64.b64decode(content) if encoding == "base64" else content.encode("utf-8")
            except Exception:
                self._error(422, "Invalid base64 content")
                return
            backend.sleep(backend.write_latency, extra=len(data) / (20 * 1024 * 1024))
            with repo.lock:
                sha = repo.put_blob(data)
            self._send(201, {"sha": sha, "url": f"/git/blobs/{sha}"})
            return

        # POST /git/trees
        if method == "POST" and rest[:2] == ["git", "trees"]:
            body = self._body()
            base_tree = body.get("base_tree")
            items = body.get("tree", [])
            with repo.lock:
                if base_tree:
                    if base_tree not in repo.trees:
                        backend.sleep(backend.write_latency)
                        self._error(422, f"Base tree {base_tree} does not exist")
                        return
                    entries = dict(repo.trees[base_tree])
                else:
                    entries = {}

                for item in items:
                    path = item.get("path")
                    mode = item.get("mode", "100644")
                    has_sha = "sha" in item
                    has_content = "content" in item
                    if has_sha and has_content:
                        self._error(422, "Cannot supply both sha and content for a tree entry")
                        return
                    if has_sha and item["sha"] is None:
                        if path not in entries:
                            self._error(422, f"Cannot delete missing path {path}")
                            return
                        del entries[path]
                        continue
                    if has_content:
                        # Real GitHub writes the blob for you; do the same, and
                        # hash the UTF-8 bytes exactly as git would.
                        data = item["content"].encode("utf-8")
                        sha = repo.put_blob(data)
                    elif has_sha:
                        sha = item["sha"]
                        if sha not in repo.blobs:
                            self._error(422, f"Blob {sha} does not exist")
                            return
                    else:
                        self._error(422, f"Tree entry for {path} needs sha or content")
                        return
                    entries[path] = (mode, sha)

                tree_sha = repo.put_tree(entries)

            backend.sleep(backend.write_latency, extra=len(items) * backend.per_entry_latency)
            self._send(
                201,
                {
                    "sha": tree_sha,
                    "tree": [
                        {"path": p, "mode": m, "type": "blob", "sha": s}
                        for p, (m, s) in sorted(entries.items())
                    ][:100],
                    "truncated": len(entries) > 100,
                },
            )
            return

        # POST /git/commits
        if method == "POST" and rest[:2] == ["git", "commits"]:
            body = self._body()
            backend.sleep(backend.write_latency)
            tree = body.get("tree")
            with repo.lock:
                if tree not in repo.trees:
                    self._error(422, f"Tree {tree} does not exist")
                    return
                sha = hashlib.sha1(
                    (json.dumps(body, sort_keys=True) + str(time.time())).encode()
                ).hexdigest()
                repo.commits[sha] = {
                    "sha": sha,
                    "message": body.get("message", ""),
                    "tree": {"sha": tree},
                    "parents": [{"sha": p} for p in body.get("parents", [])],
                }
            self._send(201, repo.commits[sha])
            return

        # PATCH /git/refs/heads/{branch}
        if method == "PATCH" and rest[:2] == ["git", "refs"] and len(rest) >= 4:
            body = self._body()
            branch = "/".join(rest[3:])
            backend.sleep(backend.write_latency)
            with repo.lock:
                repo.refs[branch] = body.get("sha", "")
                sha = repo.refs[branch]
            self._send(200, {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}})
            return

        # PUT /contents/{path}  (repository initialization)
        if method == "PUT" and rest[:1] == ["contents"]:
            body = self._body()
            path = "/".join(rest[1:])
            branch = body.get("branch", "main")
            backend.sleep(backend.write_latency)
            data = base64.b64decode(body.get("content", ""))
            with repo.lock:
                blob_sha = repo.put_blob(data)
                base = repo.trees.get(repo.commits.get(repo.refs.get(branch, ""), {}).get("tree", {}).get("sha", ""), {})
                entries = dict(base)
                entries[path] = ("100644", blob_sha)
                tree_sha = repo.put_tree(entries)
                commit_sha = hashlib.sha1(f"init{time.time()}".encode()).hexdigest()
                repo.commits[commit_sha] = {
                    "sha": commit_sha,
                    "message": body.get("message", ""),
                    "tree": {"sha": tree_sha},
                    "parents": [],
                }
                repo.refs[branch] = commit_sha
            self._send(201, {"content": {"path": path}, "commit": {"sha": commit_sha}})
            return

        # GET /_debug/stats  (test helper, not part of GitHub's API)
        if method == "GET" and rest[:2] == ["_debug", "stats"]:
            self._send(200, {
                "requests": backend.stats.total,
                "max_request_bytes": backend.stats.max_request_bytes,
                "by_endpoint": backend.stats.by_endpoint,
                "rate_limit_rejections": backend.limiter.rejections,
            })
            return

        # GET /_debug/files/{branch}  (test helper, not part of GitHub's API)
        if method == "GET" and rest[:1] == ["_debug"] and len(rest) >= 3:
            branch = rest[2]
            with repo.lock:
                commit_sha = repo.refs.get(branch)
                commit = repo.commits.get(commit_sha or "", {})
                tree_sha = commit.get("tree", {}).get("sha", "")
                files = {
                    p: {"sha": s, "mode": m, "b64": base64.b64encode(repo.blobs[s]).decode()}
                    for p, (m, s) in repo.trees.get(tree_sha, {}).items()
                }
            self._send(200, {"branch": branch, "commit": commit_sha, "files": files})
            return

        backend.sleep(backend.latency)
        self._error(404, f"Not Found: {method} /{'/'.join(rest)}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")


def serve(backend: MockGitHub, host: str = "127.0.0.1", port: int = 0):
    handler = type("BoundHandler", (Handler,), {"backend": backend})
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"


def main():
    parser = argparse.ArgumentParser(description="Mock GitHub Git Data API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--latency", type=float, default=0.12, help="simulated GET latency (s)")
    parser.add_argument("--write-latency", type=float, default=0.25, help="simulated write latency (s)")
    parser.add_argument("--points-per-min", type=int, default=900, help="0 disables rate limiting")
    args = parser.parse_args()

    backend = MockGitHub(
        latency=args.latency,
        write_latency=args.write_latency,
        points_per_min=args.points_per_min,
    )
    httpd, url = serve(backend, args.host, args.port)
    print(f"Mock GitHub API listening on {url}")
    print(f"  GET latency ~{args.latency}s, write latency ~{args.write_latency}s")
    print(f"  secondary rate limit: {args.points_per_min} points/min (POST=5, GET=1)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
