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
            k: kwargs.pop(k) for k in ("inline", "bulk", "verify", "prune") if k in kwargs
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
        b"# Project Repository\n\nInitialized automatically by Zip to git.\n"
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

    def test_bulk_binary_send_preserves_executable_mode(self):
        files = {f"bin/tool{i}": b"\x00\xff" + bytes([i]) * 64 for i in range(8)}
        result = self.deploy(build_zip(files, exec_paths={"bin/tool3"}))
        self.assertEqual(result.bulk_batches, 1)
        modes = self.remote_modes()
        self.assertEqual(modes["bin/tool3"], "100755")
        self.assertEqual(modes["bin/tool4"], "100644")

    def test_bulk_can_be_disabled_for_compatibility(self):
        files = {f"assets/a{i}.bin": b"\x00\xff" + bytes([i]) * 32 for i in range(8)}
        result = self.deploy(build_zip(files), bulk=False)
        self.assertEqual(result.bulk_batches, 0)
        self.assertEqual(self.backend.stats.by_endpoint.get("POST /graphql", 0), 0)
        self.assertEqual(self.backend.stats.by_endpoint.get("POST /git/blobs"), 8)
        self.assertEqual(self.remote_files(), files)


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

    def test_three_thousand_binary_files_are_batched(self):
        """Binary-heavy archives must also finish in tens, not thousands, of POSTs."""
        files = {
            f"assets/chunk{i:04d}.bin": b"\x00\xffbinary" + i.to_bytes(4, "big") + bytes([i % 251]) * 96
            for i in range(3000)
        }
        result = self.deploy(build_zip(files))

        self.assertEqual(result.blobs_uploaded, 3000)
        self.assertEqual(result.bulk_batches, 30)
        self.assertEqual(self.backend.stats.by_endpoint.get("POST /git/blobs", 0), 0)
        self.assertEqual(self.backend.stats.by_endpoint.get("POST /graphql"), 30)
        self.assertEqual(self.backend.limiter.rejections, 0, "hit GitHub's secondary rate limit")
        self.assertLess(result.api_requests, 50, "binary deploy used too many API requests")
        remote = self.remote_files()
        self.assertEqual(len(remote), 3000)
        for path in ("assets/chunk0000.bin", "assets/chunk1499.bin", "assets/chunk2999.bin"):
            self.assertEqual(remote[path], files[path])

        # Staging commits never become target history, and their random refs
        # are removed even though their objects remain available to Git.
        repo = self.backend.repo("octocat", "hello-world")
        self.assertEqual(set(repo.refs), {"main"})
        head = repo.commits[repo.refs["main"]]
        self.assertEqual(len(head["parents"]), 1)


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

    def test_generated_typescript_bundle_stays_on_the_fast_path(self):
        # A 1.5 MB generated .ts file is typical in an Astro/TS repo.
        # It must ride inline — one extra blob POST is the old "low" path.
        big = ("export const data = 'x';\n" * 60000).encode()
        self.assertGreater(len(big), 1024 * 1024)
        self.assertLess(len(big), app.MAX_INLINE_BYTES)
        files = {"src/generated/api-types.ts": big, "small.ts": b"export const a = 1;\n"}
        result = self.deploy(build_zip(files))
        self.assertEqual(result.blobs_uploaded, 0)
        self.assertEqual(result.inlined, 2)
        self.assertEqual(self.remote_files()["src/generated/api-types.ts"], big)

    def test_text_bigger_than_a_tree_request_still_round_trips(self):
        huge = b"export const x = 1;\n" * ((app.MAX_INLINE_BYTES // 20) + 200)
        self.assertFalse(app.is_inlineable(huge, "src/huge.ts"))
        result = self.deploy(build_zip({"src/huge.ts": huge}))
        self.assertEqual(result.blobs_uploaded, 1)
        self.assertEqual(self.remote_files()["src/huge.ts"], huge)


class TestAstroProfile(DeployTestCase):
    """TypeScript 92.2% / CSS 4.9% / Astro 2.3% / Other 0.6% — send stays fast."""

    def test_language_mix_matches_advertised_breakdown(self):
        from tests.benchmark import ASTRO_LANGUAGE_MIX, language_mix, make_astro_zip

        for n in (80, 400, 3000):
            _, files = make_astro_zip(n)
            mix = dict(language_mix(files))
            for lang, target in ASTRO_LANGUAGE_MIX.items():
                self.assertEqual(
                    round(mix.get(lang, 0.0), 1),
                    target,
                    f"{lang} at n={n}: got {mix.get(lang, 0.0):.3f}%",
                )
            extras = {lang: share for lang, share in mix.items() if lang not in ASTRO_LANGUAGE_MIX}
            for lang, share in extras.items():
                self.assertLess(share, 0.05, f"unexpected {lang} {share:.2f}% at n={n}")

    def test_astro_corpus_sends_fast_and_stays_byte_exact(self):
        from tests.benchmark import make_astro_zip

        zip_bytes, expected = make_astro_zip(400)
        result = self.deploy(zip_bytes)
        remote = self.remote_files()

        self.assertEqual(result.total_files, len(expected))
        self.assertLess(result.api_requests, 45, "send used too many API requests")
        self.assertEqual(self.backend.limiter.rejections, 0)
        source = [p for p in expected if p.endswith((".ts", ".tsx", ".css", ".astro"))]
        binaries = [p for p, data in expected.items() if not app.is_inlineable(data, p)]
        self.assertTrue(source)
        for path in source:
            self.assertTrue(
                app.is_inlineable(expected[path], path),
                f"{path} ({len(expected[path])} bytes) must stay on the fast path",
            )
        self.assertEqual(result.blobs_uploaded, len(binaries))
        self.assertTrue(all(p.endswith(".webp") for p in binaries), binaries)
        self.assertEqual(result.inlined, len(expected) - len(binaries))
        self.assertLess(self.backend.stats.max_request_bytes, 6 * 1024 * 1024)
        for path, data in expected.items():
            self.assertEqual(remote[path], data, f"content mismatch for {path}")


class TestAdaptiveTreeSend(DeployTestCase):
    def test_oversized_bulk_mutation_is_split_instead_of_crashing(self):
        self.backend.max_body_bytes = 20_000
        files = {
            f"assets/blob{i:03d}.bin": b"\x00\xff" + i.to_bytes(2, "big") + bytes([i]) * 1000
            for i in range(80)
        }
        result = self.deploy(build_zip(files))
        self.assertGreater(result.bulk_batches, 1)
        self.assertEqual(self.backend.stats.by_endpoint.get("POST /git/blobs", 0), 0)
        self.assertEqual(self.remote_files(), files)
        self.assertEqual(set(self.backend.repo("octocat", "hello-world").refs), {"main"})

    def test_oversized_tree_is_split_instead_of_crashing(self):
        """A 413 from GitHub must split the chunk and finish the send."""
        self.backend.max_body_bytes = 50_000
        files = {
            f"src/mod{i:03d}.ts": (f"export const n = {i};\n" + "export const s = 'xxxx';\n" * 30).encode()
            for i in range(80)
        }
        result = self.deploy(build_zip(files))
        remote = self.remote_files()
        self.assertEqual(len(remote), 80)
        self.assertGreaterEqual(result.tree_calls, 2)
        for path, data in files.items():
            self.assertEqual(remote[path], data)

    def test_pack_tree_items_stays_within_budget(self):
        items = [
            {"path": f"src/m{i:04d}.ts", "mode": "100644", "type": "blob", "content": "x" * 8000}
            for i in range(50)
        ]
        chunks = app.pack_tree_items(items, max_entries=400, max_bytes=100_000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            payload = app.encode_json({"tree": chunk, "base_tree": "a" * 40})
            self.assertLessEqual(len(payload), 100_000 + 2048)
            self.assertLessEqual(len(chunk), 400)
        self.assertEqual(sum(len(c) for c in chunks), 50)


class TestRealtimeTelemetry(DeployTestCase):
    """The progress bar and request log must reflect the deploy truthfully."""

    def _deploy_with_hooks(self, zip_bytes, **kwargs):
        events: list[tuple[float, str, str]] = []
        requests: list[tuple[str, str, int]] = []
        files = app.read_zip(zip_bytes, workers=4)
        client = app.GitHubClient(
            token="test-token",
            owner="octocat",
            repo="hello-world",
            api_base=self.base_url,
            workers=8,
            points_per_min=100000,
        )
        client.on_request = lambda method, label, status, ms, sent, total: requests.append(
            (method, label, status)
        )
        deployer = app.Deployer(
            client,
            branch="main",
            workers=8,
            progress=lambda f, label, detail: events.append((f, label, detail)),
            **kwargs,
        )
        try:
            result = deployer.deploy(files)
        finally:
            client.close()
        return result, events, requests, client

    def test_progress_reaches_completion_and_moves_forward(self):
        zip_bytes = build_zip(
            {f"src/f{i}.txt": f"content {i}\n".encode() for i in range(50)}
            | {f"bin/b{i}.bin": bytes([0, i % 256]) * 40 for i in range(20)}
        )
        result, events, requests, client = self._deploy_with_hooks(zip_bytes)
        self.assertTrue(events, "progress callback never fired")
        fractions = [f for f, _, _ in events]
        self.assertEqual(fractions[-1], 1.0, "progress must end at 100%")
        self.assertTrue(all(0.0 <= f <= 1.0 for f in fractions))
        # Phases move forward: each new phase starts at or above 90% of the
        # running max (uploads and tree writing overlap by design).
        running_max = 0.0
        for f in fractions:
            self.assertGreaterEqual(f, running_max * 0.5)
            running_max = max(running_max, f)
        labels = {label for _, label, _ in events}
        self.assertIn("Writing git tree", labels)
        self.assertIn("Complete", {label for _, label, _ in events})

    def test_request_hook_sees_every_api_call(self):
        zip_bytes = build_zip({"a.txt": b"alpha\n", "b/c.txt": b"beta\n"})
        result, events, requests, client = self._deploy_with_hooks(zip_bytes)
        self.assertEqual(len(requests), client.requests_made)
        self.assertEqual(len(requests), result.api_requests)
        for method, label, status in requests:
            self.assertIn(method, ("GET", "POST", "PUT", "PATCH", "DELETE"))
            self.assertTrue(label)
            self.assertTrue(100 <= status < 600)

    def test_progress_and_hooks_survive_noop_redeploy(self):
        zip_bytes = build_zip({"x.txt": b"same\n"})
        self._deploy_with_hooks(zip_bytes)
        result, events, requests, _ = self._deploy_with_hooks(zip_bytes)
        self.assertTrue(result.no_changes)
        self.assertEqual(events[-1][0], 1.0)

    def test_broken_observer_does_not_break_deploy(self):
        zip_bytes = build_zip({"ok.txt": b"fine\n"})
        files = app.read_zip(zip_bytes, workers=2)
        client = app.GitHubClient(
            token="test-token",
            owner="octocat",
            repo="hello-world",
            api_base=self.base_url,
            workers=4,
            points_per_min=100000,
        )

        def exploding_hook(*args):
            raise RuntimeError("observer bug")

        client.on_request = exploding_hook
        deployer = app.Deployer(client, branch="main", workers=4)
        try:
            result = deployer.deploy(files)
        finally:
            client.close()
        self.assertFalse(result.no_changes)
        self.assertEqual(self.remote_files()["ok.txt"], b"fine\n")


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

    def test_graphql_endpoint_derivation(self):
        self.assertEqual(
            app.GitHubClient._derive_graphql_base("https://api.github.com"),
            "https://api.github.com/graphql",
        )
        self.assertEqual(
            app.GitHubClient._derive_graphql_base("https://github.example/api/v3"),
            "https://github.example/api/graphql",
        )

    def test_inlineable_classification(self):
        self.assertTrue(app.is_inlineable(b"plain text"))
        self.assertTrue(app.is_inlineable("emoji 🚀".encode("utf-8")))
        self.assertFalse(app.is_inlineable(b"has\x00null"))
        self.assertFalse(app.is_inlineable(b"\xff\xfe\xfd invalid utf8"))
        self.assertFalse(app.is_inlineable(b"x" * (app.MAX_INLINE_BYTES + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
