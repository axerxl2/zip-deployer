#!/usr/bin/env python3
"""
Correctness tests for the fast deployer, run against the strict mock GitHub API.

The important property under test is that going fast did not change *what*
gets deployed: every byte that goes into the ZIP must come back out of the
repository unchanged, including binaries, unicode, CRLF endings, empty files
and executable bits.

    python tests/test_deploy.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import unittest
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from tests.mock_github import MockGitHub, serve  # noqa: E402

app.QUIET = True


def build_zip(files: dict[str, bytes], exec_paths: set[str] = frozenset()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            info = zipfile.ZipInfo(path)
            info.create_system = 3  # unix
            mode = 0o755 if path in exec_paths else 0o644
            info.external_attr = (mode & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buffer.getvalue()


class DeployTestCase(unittest.TestCase):
    latency = 0.0
    fail_every = 0
    points_per_min = 0  # disabled unless a test asks for it

    def setUp(self):
        self.backend = MockGitHub(
            latency=self.latency,
            write_latency=self.latency,
            per_entry_latency=0.0,
            jitter=0.0,
            points_per_min=self.points_per_min,
            fail_every=self.fail_every,
        )
        self.httpd, self.base_url = serve(self.backend)
        self.addCleanup(self.httpd.shutdown)

    def deploy(self, zip_bytes: bytes, branch="main", **kwargs):
        deploy_kwargs = {
            k: kwargs.pop(k) for k in ("inline", "verify", "prune") if k in kwargs
        }
        files = app.read_zip(zip_bytes, workers=4, strip_root=kwargs.pop("strip_root", False))
        client = app.GitHubClient(
            token="test-token",
            owner="octocat",
            repo="hello-world",
            api_base=self.base_url,
            workers=8,
            points_per_min=100000,
        )
        deployer = app.Deployer(client, branch=branch, workers=8, **deploy_kwargs)
        try:
            return deployer.deploy(files, **kwargs)
        finally:
            client.close()

    BOOTSTRAP_README = (
        b"# Project Repository\n\nInitialized automatically by GitHub ZIP Deployer.\n"
    )

    def remote_files(self, branch="main") -> dict[str, bytes]:
        """Files on the branch, minus the auto-generated bootstrap README."""
        files = self.all_remote_files(branch)
        if files.get("README.md") == self.BOOTSTRAP_README:
            del files["README.md"]
        return files

    def all_remote_files(self, branch="main") -> dict[str, bytes]:
        url = f"{self.base_url}/repos/octocat/hello-world/_debug/files/{branch}"
        with urllib.request.urlopen(url) as response:
            payload = json.loads(response.read().decode())
        return {p: base64.b64decode(v["b64"]) for p, v in payload["files"].items()}

    def remote_modes(self, branch="main") -> dict[str, str]:
        url = f"{self.base_url}/repos/octocat/hello-world/_debug/files/{branch}"
        with urllib.request.urlopen(url) as response:
            payload = json.loads(response.read().decode())
        return {p: v["mode"] for p, v in payload["files"].items()}


class TestContentFidelity(DeployTestCase):
    """Every byte must survive the round trip."""

    TRICKY = {
        "README.md": b"# Hello\n\nPlain ascii text.\n",
        "src/app.js": b"console.log('hi');\n",
        "src/nested/deep/config.json": b'{\n  "a": 1\n}\n',
        "docs/unicode.md": "# Ünïcödé — emoji 🚀 and CJK 日本語\n".encode("utf-8"),
        "docs/crlf.txt": b"line one\r\nline two\r\n",
        "docs/no-trailing-newline.txt": b"no newline at end",
        "docs/empty.txt": b"",
        "docs/whitespace.txt": b"   \n\t\n  \n",
        "assets/logo.png": bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(range(256)) * 4,
        "assets/data.bin": os.urandom(5000),
        "assets/nulls.txt": b"has\x00null\x00bytes",
        "scripts/build.sh": b"#!/bin/sh\necho building\n",
        "weird name (1).txt": b"spaces and parens\n",
        "ünïcode-filename.txt": "unicode in the filename\n".encode("utf-8"),
    }

    def test_all_content_round_trips_exactly(self):
        zip_bytes = build_zip(self.TRICKY, exec_paths={"scripts/build.sh"})
        result = self.deploy(zip_bytes)
        remote = self.remote_files()

        self.assertEqual(result.total_files, len(self.TRICKY))
        self.assertEqual(set(remote), set(self.TRICKY))
        for path, expected in self.TRICKY.items():
            self.assertEqual(remote[path], expected, f"content mismatch for {path}")

    def test_executable_bit_preserved(self):
        zip_bytes = build_zip(self.TRICKY, exec_paths={"scripts/build.sh"})
        self.deploy(zip_bytes)
        modes = self.remote_modes()
        self.assertEqual(modes["scripts/build.sh"], "100755")
        self.assertEqual(modes["README.md"], "100644")

    def test_inline_and_blob_paths_agree(self):
        """Turning off the fast inline path must produce identical content."""
        zip_bytes = build_zip(self.TRICKY)
        self.deploy(zip_bytes)
        fast = self.remote_files()

        self.setUp()  # fresh backend
        self.deploy(zip_bytes, inline=False)
        slow = self.remote_files()

        self.assertEqual(fast, slow)

    def test_binary_files_take_the_blob_path(self):
        zip_bytes = build_zip(self.TRICKY)
        result = self.deploy(zip_bytes)
        # png, random bin and the NUL-containing file cannot be inlined
        self.assertEqual(result.blobs_uploaded, 3)
        self.assertEqual(result.inlined, len(self.TRICKY) - 3)


class TestIncrementalDeploys(DeployTestCase):
    def test_unchanged_redeploy_is_a_no_op(self):
        zip_bytes = build_zip({"a.txt": b"one\n", "b.txt": b"two\n", "bin": os.urandom(100)})
        self.deploy(zip_bytes)
        requests_after_first = self.backend.stats.total

        result = self.deploy(zip_bytes)
        self.assertTrue(result.no_changes)
        self.assertEqual(result.unchanged, 3)
        # A no-op deploy costs only the handful of reads needed to compare.
        self.assertLessEqual(self.backend.stats.total - requests_after_first, 4)

    def test_only_changed_files_are_written(self):
        original = {f"file{i}.txt": f"content {i}\n".encode() for i in range(50)}
        self.deploy(build_zip(original))

        updated = dict(original)
        updated["file7.txt"] = b"changed!\n"
        result = self.deploy(build_zip(updated))

        self.assertEqual(result.unchanged, 49)
        self.assertEqual(result.inlined, 1)
        self.assertEqual(self.remote_files()["file7.txt"], b"changed!\n")

    def test_duplicate_content_is_uploaded_once(self):
        payload = os.urandom(2048)
        files = {f"copy{i}.bin": payload for i in range(10)}
        result = self.deploy(build_zip(files))
        self.assertEqual(result.blobs_uploaded, 1)  # deduplicated by content SHA
        remote = self.remote_files()
        self.assertEqual(len(remote), 10)
        self.assertTrue(all(v == payload for v in remote.values()))

    def test_prune_removes_files_absent_from_zip(self):
        self.deploy(build_zip({"keep.txt": b"keep\n", "drop.txt": b"drop\n"}))
        self.deploy(build_zip({"keep.txt": b"keep\n"}), prune=True)
        self.assertEqual(set(self.remote_files()), {"keep.txt"})

    def test_without_prune_existing_files_survive(self):
        self.deploy(build_zip({"keep.txt": b"keep\n", "drop.txt": b"drop\n"}))
        self.deploy(build_zip({"keep.txt": b"keep\n"}))
        self.assertEqual(set(self.remote_files()), {"keep.txt", "drop.txt"})


class TestPathHandling(DeployTestCase):
    def test_junk_paths_are_filtered(self):
        files = {
            "real.txt": b"real\n",
            "__MACOSX/._real.txt": b"junk",
            "folder/.DS_Store": b"junk",
            ".git/config": b"[core]\n",
            "nested/../escape.txt": b"nope",
        }
        self.deploy(build_zip(files))
        self.assertEqual(set(self.remote_files()), {"real.txt"})

    def test_strip_root_removes_wrapper_folder(self):
        files = {"my-project/index.html": b"<h1>hi</h1>", "my-project/src/a.js": b"1\n"}
        self.deploy(build_zip(files), strip_root=True)
        self.assertEqual(set(self.remote_files()), {"index.html", "src/a.js"})

    def test_strip_root_left_alone_when_multiple_roots(self):
        files = {"a/one.txt": b"1", "b/two.txt": b"2"}
        self.deploy(build_zip(files), strip_root=True)
        self.assertEqual(set(self.remote_files()), {"a/one.txt", "b/two.txt"})

    def test_windows_separators_are_normalized(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("src\\win\\file.txt", b"windows\n")
        self.deploy(buffer.getvalue())
        self.assertEqual(set(self.remote_files()), {"src/win/file.txt"})


class TestRepositoryBootstrap(DeployTestCase):
    def test_empty_repository_is_initialized(self):
        result = self.deploy(build_zip({"index.html": b"<h1>new</h1>"}))
        remote = self.all_remote_files()
        self.assertIn("index.html", remote)
        self.assertIn("README.md", remote)  # created by the bootstrap commit
        self.assertTrue(result.commit_sha)

    def test_deploy_to_a_non_default_branch(self):
        self.deploy(build_zip({"x.txt": b"x\n"}), branch="staging")
        self.assertIn("x.txt", self.remote_files("staging"))


class TestResilience(DeployTestCase):
    fail_every = 3  # every third request fails with a transient 500

    def test_survives_flaky_api(self):
        files = {f"f{i}.txt": f"line {i}\n".encode() for i in range(40)}
        files["blob.bin"] = os.urandom(1000)
        result = self.deploy(build_zip(files))
        self.assertEqual(result.total_files, 41)
        self.assertGreater(self.backend.injected_failures, 0)
        remote = self.remote_files()
        for path, expected in files.items():
            self.assertEqual(remote[path], expected)


class TestRateLimitCompliance(DeployTestCase):
    points_per_min = 900

    def test_three_thousand_files_stay_within_the_point_budget(self):
        """The whole point: 3k files must not blow the 900 points/min budget."""
        files = {f"src/mod{i:04d}.js": f"export const n = {i};\n".encode() for i in range(3000)}
        result = self.deploy(build_zip(files))
        self.assertEqual(result.total_files, 3000)
        self.assertEqual(self.backend.limiter.rejections, 0, "hit GitHub's secondary rate limit")
        self.assertLess(self.backend.stats.total, 60, "used too many API requests")
        self.assertEqual(len(self.remote_files()), 3000)


class TestPayloadBounds(DeployTestCase):
    """Packing many files per request must not produce oversized payloads."""

    def test_cjk_content_stays_within_the_payload_budget(self):
        # Pure CJK + emoji: 3-4 bytes per character, and the worst case for
        # any size estimate that counts characters instead of bytes.
        body = ("日本語のページ 🚀 Ünïcödé\n" * 200).encode("utf-8")
        files = {f"src/pages/page{i:04d}.astro": body for i in range(400)}
        # Vary content so deduplication does not hide the problem.
        files = {
            f"src/pages/page{i:04d}.astro": body + f"// {i}\n".encode()
            for i in range(400)
        }
        self.deploy(build_zip(files))
        largest = self.backend.stats.max_request_bytes
        self.assertLess(largest, 6 * 1024 * 1024,
                        f"tree payload grew to {largest / 1e6:.1f} MB")
        remote = self.remote_files()
        self.assertEqual(len(remote), 400)
        for path, expected in files.items():
            self.assertEqual(remote[path], expected, f"CJK content mangled for {path}")

    def test_large_text_file_round_trips_via_blob(self):
        # Above MAX_INLINE_BYTES a text file must switch to a blob upload —
        # lockfiles, bundles and source maps in real projects land here.
        big = ("export const data = 'x';\n" * 60000).encode()
        self.assertGreater(len(big), app.MAX_INLINE_BYTES)
        files = {"src/generated/api-types.ts": big, "small.ts": b"export const a = 1;\n"}
        result = self.deploy(build_zip(files))
        self.assertEqual(result.blobs_uploaded, 1)   # only the big one
        self.assertEqual(result.inlined, 1)
        self.assertEqual(self.remote_files()["src/generated/api-types.ts"], big)


class TestLocalHelpers(unittest.TestCase):
    def test_git_blob_sha_matches_git(self):
        # `printf 'hello' | git hash-object --stdin` -> b6fc4c62...
        self.assertEqual(
            app.git_blob_sha(b"hello"), "b6fc4c620b67d95f953a5c1c1230aaab5db5a1b0"
        )
        # empty blob is a well-known constant
        self.assertEqual(
            app.git_blob_sha(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
        )

    def test_inlineable_classification(self):
        self.assertTrue(app.is_inlineable(b"plain text"))
        self.assertTrue(app.is_inlineable("emoji 🚀".encode("utf-8")))
        self.assertFalse(app.is_inlineable(b"has\x00null"))
        self.assertFalse(app.is_inlineable(b"\xff\xfe\xfd invalid utf8"))
        self.assertFalse(app.is_inlineable(b"x" * (app.MAX_INLINE_BYTES + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
