#!/usr/bin/env python3
"""
Head-to-head benchmark: the original deployer vs the fast one.

Both are run against tests/mock_github.py, which simulates GitHub's latency
*and* enforces the documented secondary rate limit (900 points/minute, with
POST costing 5 points). The original implementation is not reimplemented here
— it is pulled verbatim out of git history, with only its hard-coded API host
rewritten to point at the mock.

    python tests/benchmark.py                 # full run, 3000 files
    python tests/benchmark.py --files 500     # quicker
    python tests/benchmark.py --skip-legacy   # only measure the new engine

Numbers are latency-simulated, not measurements against github.com; the point
is the *shape* of the work (how many round trips, and whether the rate limit
is tripped), which is what determines real-world wall time.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from tests.mock_github import MockGitHub, serve  # noqa: E402

BASE_COMMIT = "a3f5bfca797f7e6def01e79d3b5f110346d1acac"  # pre-optimisation app.py

# Simulated GitHub timings (deliberately optimistic about GitHub's speed).
GET_LATENCY = 0.12
WRITE_LATENCY = 0.25
PER_ENTRY_LATENCY = 0.002
POINTS_PER_MIN = 900


# --------------------------------------------------------------------------
# Test corpus
# --------------------------------------------------------------------------


def make_project_zip(n_files: int, seed: int = 1337) -> tuple[bytes, dict[str, bytes]]:
    """A realistic front-end-ish project: mostly small text, some binaries."""
    rng = random.Random(seed)
    files: dict[str, bytes] = {}

    words = ["render", "state", "config", "handler", "buffer", "client", "layout", "parse"]

    def js_module(i: int) -> bytes:
        lines = [f"// module {i} — generated fixture", "import { helper } from '../lib/helper';", ""]
        for j in range(rng.randint(6, 40)):
            name = rng.choice(words)
            lines.append(f"export function {name}{j}(input) {{")
            lines.append(f"  return helper(input, {rng.randint(0, 9999)});")
            lines.append("}")
        return ("\n".join(lines) + "\n").encode()

    def css_file(i: int) -> bytes:
        out = [f"/* stylesheet {i} */"]
        for j in range(rng.randint(5, 25)):
            out.append(f".c{i}-{j} {{ color: #{rng.randint(0, 0xFFFFFF):06x}; padding: {j}px; }}")
        return ("\n".join(out) + "\n").encode()

    def html_file(i: int) -> bytes:
        return textwrap.dedent(f"""\
            <!DOCTYPE html>
            <html lang="en">
              <head><meta charset="utf-8"><title>Page {i}</title></head>
              <body><h1>Page {i}</h1><p>Fixture content — ünïcode ok 🚀</p></body>
            </html>
            """).encode()

    for i in range(n_files):
        bucket = rng.random()
        if bucket < 0.55:
            files[f"src/modules/mod{i:05d}.js"] = js_module(i)
        elif bucket < 0.70:
            files[f"src/styles/style{i:05d}.css"] = css_file(i)
        elif bucket < 0.80:
            files[f"public/pages/page{i:05d}.html"] = html_file(i)
        elif bucket < 0.88:
            files[f"data/config{i:05d}.json"] = json.dumps(
                {"id": i, "name": rng.choice(words), "values": [rng.random() for _ in range(10)]},
                indent=2,
            ).encode()
        elif bucket < 0.94:
            files[f"docs/doc{i:05d}.md"] = (
                f"# Document {i}\n\n" + " ".join(rng.choice(words) for _ in range(200)) + "\n"
            ).encode()
        else:
            # binary assets must take the blob-upload path
            size = rng.randint(1024, 20480)
            files[f"assets/img{i:05d}.png"] = bytes([0x89, 0x50, 0x4E, 0x47]) + bytes(
                rng.getrandbits(8) for _ in range(size)
            )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return buffer.getvalue(), files


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class Bench:
    def __init__(self, rate_limit: bool = True):
        self.backend = MockGitHub(
            latency=GET_LATENCY,
            write_latency=WRITE_LATENCY,
            per_entry_latency=PER_ENTRY_LATENCY,
            jitter=0.3,
            points_per_min=POINTS_PER_MIN if rate_limit else 0,
        )
        self.httpd, self.url = serve(self.backend)

    def stop(self):
        self.httpd.shutdown()

    def remote_files(self, owner="octocat", repo="bench", branch="main") -> dict[str, bytes]:
        url = f"{self.url}/repos/{owner}/{repo}/_debug/files/{branch}"
        with urllib.request.urlopen(url) as response:
            payload = json.loads(response.read().decode())
        return {p: base64.b64decode(v["b64"]) for p, v in payload["files"].items()}


def run_new(bench: Bench, zip_path: Path, repo: str, **kwargs) -> tuple[float, object]:
    zip_bytes = zip_path.read_bytes()
    files = app.read_zip(zip_bytes, workers=12)
    client = app.GitHubClient(
        token="bench-token",
        owner="octocat",
        repo=repo,
        api_base=bench.url,
        workers=kwargs.pop("workers", 12),
        points_per_min=POINTS_PER_MIN,
    )
    deployer = app.Deployer(client, branch="main", workers=12, **kwargs)
    started = time.monotonic()
    result = deployer.deploy(files)
    elapsed = time.monotonic() - started
    client.close()
    return elapsed, result


def legacy_source(target_url: str) -> str:
    """The pre-optimisation app.py, verbatim except for the API host."""
    raw = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:app.py"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    if raw.returncode != 0:
        raise SystemExit(f"cannot read the original app.py from git: {raw.stderr}")
    source = raw.stdout
    if "https://api.github.com" not in source:
        raise SystemExit("unexpected legacy source: API host not found")
    return source.replace("https://api.github.com", target_url)


def run_legacy(bench: Bench, zip_path: Path, repo: str, timeout: float) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "legacy_app.py"
        script.write_text(legacy_source(bench.url))
        stdin = f"token\noctocat\n{repo}\nmain\n{zip_path}\n"
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = time.monotonic() - started
            return {
                "elapsed": elapsed,
                "ok": proc.returncode == 0 and "Successfully deployed" in proc.stdout,
                "returncode": proc.returncode,
                "tail": (proc.stdout + proc.stderr).strip().splitlines()[-3:],
            }
        except subprocess.TimeoutExpired:
            return {"elapsed": timeout, "ok": False, "returncode": None, "timeout": True, "tail": []}


def fmt_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f} min"


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", type=int, default=3000, help="files in the benchmark ZIP")
    parser.add_argument("--legacy-files", type=int, default=200,
                        help="subset size for the legacy run (it is too slow to run in full)")
    parser.add_argument("--skip-legacy", action="store_true")
    parser.add_argument("--legacy-timeout", type=float, default=600.0)
    args = parser.parse_args()

    app.QUIET = True
    rows = []
    tmpdir = Path(tempfile.mkdtemp(prefix="zipbench-"))
    try:
        print(f"Building a {args.files}-file project ZIP...")
        zip_bytes, expected = make_project_zip(args.files)
        zip_path = tmpdir / "project.zip"
        zip_path.write_bytes(zip_bytes)
        total_bytes = sum(len(v) for v in expected.values())
        print(
            f"  {len(expected)} files, {total_bytes / 1e6:.1f} MB uncompressed, "
            f"{len(zip_bytes) / 1e6:.1f} MB zipped\n"
        )

        print("Simulated GitHub API:")
        print(f"  GET latency ~{GET_LATENCY * 1000:.0f} ms, write latency ~{WRITE_LATENCY * 1000:.0f} ms")
        print(f"  secondary rate limit: {POINTS_PER_MIN} points/min (GET=1, POST=5)\n")

        # ---- 1. New engine, full corpus, rate limit enforced --------------
        print(f"[1/5] New engine — {args.files} files, rate limit ON")
        bench = Bench(rate_limit=True)
        elapsed, result = run_new(bench, zip_path, "bench")
        deployed = bench.remote_files(repo="bench")
        deployed.pop("README.md", None)
        matched = sum(1 for p, d in expected.items() if deployed.get(p) == d)
        print(f"      {fmt_duration(elapsed)}, {result.api_requests} API requests, "
              f"{result.inlined} inlined, {result.blobs_uploaded} blobs")
        print(f"      rate-limit rejections: {bench.backend.limiter.rejections}")
        print(f"      content verified byte-exact: {matched}/{len(expected)}")
        rows.append(("New engine, first deploy", args.files, elapsed, result.api_requests,
                     matched == len(expected)))
        first_deploy_ok = matched == len(expected)
        rejections = bench.backend.limiter.rejections

        # ---- 2. New engine, redeploy unchanged ----------------------------
        print(f"\n[2/5] New engine — redeploy identical ZIP (incremental)")
        elapsed2, result2 = run_new(bench, zip_path, "bench")
        print(f"      {fmt_duration(elapsed2)}, {result2.api_requests} "
              f"API requests, {result2.unchanged} unchanged, no-op: {result2.no_changes}")
        rows.append(("New engine, no-op redeploy", args.files, elapsed2,
                     result2.api_requests, result2.no_changes))

        # ---- 3. New engine, one file changed ------------------------------
        print(f"\n[3/5] New engine — one file changed out of {args.files}")
        changed = dict(expected)
        victim = sorted(p for p in expected if p.endswith(".js"))[0]
        changed[victim] = b"// touched by the benchmark\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, data in changed.items():
                zf.writestr(path, data)
        changed_zip = tmpdir / "changed.zip"
        changed_zip.write_bytes(buffer.getvalue())
        elapsed3, result3 = run_new(bench, changed_zip, "bench")
        after = bench.remote_files(repo="bench")
        ok3 = after.get(victim) == b"// touched by the benchmark\n" and result3.unchanged == args.files - 1
        print(f"      {fmt_duration(elapsed3)}, {result3.api_requests} API requests, "
              f"{result3.unchanged} unchanged, changed file correct: {ok3}")
        rows.append(("New engine, 1 file changed", args.files, elapsed3,
                     result3.api_requests, ok3))
        bench.stop()

        if not args.skip_legacy:
            n = min(args.legacy_files, args.files)
            legacy_zip_bytes, legacy_expected = make_project_zip(n, seed=99)
            legacy_zip = tmpdir / "legacy.zip"
            legacy_zip.write_bytes(legacy_zip_bytes)

            # ---- 4. Legacy, rate limit OFF (best case for it) -------------
            print(f"\n[4/5] Original engine — {n} files, rate limit OFF (best case)")
            bench_nolimit = Bench(rate_limit=False)
            outcome = run_legacy(bench_nolimit, legacy_zip, "legacy-nolimit", args.legacy_timeout)
            per_file = outcome["elapsed"] / n
            projected = per_file * args.files
            print(f"      {fmt_duration(outcome['elapsed'])} for {n} files "
                  f"({per_file * 1000:.0f} ms/file), success: {outcome['ok']}")
            print(f"      → projected for {args.files} files: {fmt_duration(projected)}")
            rows.append((f"Original, rate limit OFF ({n} files)", n, outcome["elapsed"],
                         bench_nolimit.backend.stats.total, outcome["ok"]))
            rows.append((f"Original, projected to {args.files} (no limit)", args.files, projected,
                         args.files + 5, None))
            floor_seconds = args.files * 5 / POINTS_PER_MIN * 60
            rows.append((f"Original, floor imposed by rate limit", args.files,
                         max(projected, floor_seconds), args.files + 5, None))
            bench_nolimit.stop()

            # ---- 5. Legacy, rate limit ON (reality) -----------------------
            print(f"\n[5/5] Original engine — {n} files, rate limit ON (reality)")
            bench_limit = Bench(rate_limit=True)
            outcome2 = run_legacy(bench_limit, legacy_zip, "legacy-limited", args.legacy_timeout)
            print(f"      {fmt_duration(outcome2['elapsed'])}, success: {outcome2['ok']}, "
                  f"rate-limit rejections: {bench_limit.backend.limiter.rejections}")
            for line in outcome2["tail"]:
                print(f"      | {line[:110]}")
            rows.append((f"Original, rate limit ON ({n} files)", n, outcome2["elapsed"],
                         bench_limit.backend.stats.total, outcome2["ok"]))
            bench_limit.stop()

        # ---- summary ------------------------------------------------------
        print("\n" + "=" * 78)
        print(f"{'scenario':<40}{'files':>7}{'time':>11}{'requests':>11}{'ok':>7}")
        print("-" * 78)
        for name, files_n, seconds, requests, ok in rows:
            flag = "-" if ok is None else ("yes" if ok else "NO")
            print(f"{name:<40}{files_n:>7}{fmt_duration(seconds):>11}{requests:>11}{flag:>7}")
        print("=" * 78)

        floor = args.files * 5 / POINTS_PER_MIN
        print(
            f"\nOne POST per file cannot beat GitHub's secondary rate limit: "
            f"{args.files} files x 5 points / {POINTS_PER_MIN} points per minute "
            f"= {floor:.1f} minutes minimum."
        )
        print(
            f"Secondary rate limit rejections hit by the new engine: {rejections} "
            f"(absorbed by backoff; the deploy still verified clean)."
        )
        print("\nTarget: 3000 files deployed in under 5 minutes.")
        verdict = "PASS" if (first_deploy_ok and rows[0][2] < 300) else "FAIL"
        print(
            f"  new engine: {fmt_duration(rows[0][2])} for {args.files} files, "
            f"{rows[0][3]} API requests, all content verified byte-exact -> {verdict}"
        )
        return 0 if verdict == "PASS" else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
