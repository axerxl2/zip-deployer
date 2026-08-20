# GitHub ZIP Deployer

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](app.py)
[![Go](https://img.shields.io/badge/Go-1.16+-00ADD8.svg)](app.go)
[![C++](https://img.shields.io/badge/C++-17-blue.svg)](app.cpp)
[![Erlang](https://img.shields.io/badge/Erlang-22+-red.svg)](app.erl)
[![Ruby](https://img.shields.io/badge/Ruby-2.7+-red.svg)](app.rb)
[![PHP](https://img.shields.io/badge/PHP-7.4+-777BB4.svg)](index.php)
[![Lua](https://img.shields.io/badge/Lua-5.3+-blue.svg)](app.lua)
[![Io](https://img.shields.io/badge/Io-2017+-gray.svg)](app.io)
[![Rebol](https://img.shields.io/badge/Rebol-2.7+-orange.svg)](app.reb)
[![Haskell](https://img.shields.io/badge/Haskell-9.0+-5e5086.svg)](app.hs)
[![Forth](https://img.shields.io/badge/Forth-Gforth-000000.svg)](app.fs)

Extract a ZIP archive and push its contents straight to a GitHub repository through
the GitHub API — no clone, no git binary, one commit.

**3,000 mixed files deploy in about 14 seconds; 3,000 binary files in about 28 seconds.**

Those are latency-simulated benchmark results, not a promise for every connection or archive;
wall time also depends on total bytes and upload bandwidth. Both stay comfortably inside the
requested 1–3 minute window for the benchmark corpora.

## Why it is fast

The obvious way to do this is one `POST /git/blobs` request per file. That design has a
ceiling it cannot cross, and it is not bandwidth or concurrency:

> GitHub's secondary rate limit allows **900 points per minute**, and **every POST costs
> 5 points** — a hard cap of **180 POSTs per minute**.
> ([rate limit docs](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api))

One request per file therefore needs **at least 16.7 minutes for 3,000 files**, no matter
how many threads you throw at it. Adding concurrency just makes you hit `403 secondary
rate limit` sooner.

The fast path avoids the per-file request entirely:

| Technique | Effect |
|---|---|
| **Inline text in tree entries** — the [tree API](https://docs.github.com/en/rest/git/trees#create-a-tree) accepts `content` per entry and writes the blob | Thousands of source files collapse into a few POSTs |
| **Bulk binary mutations** — GraphQL [`createCommitOnBranch`](https://docs.github.com/en/graphql/reference/mutations#createcommitonbranch) accepts RFC 4648 base64, including binary bytes | Up to 100 binary files ride in each mutation |
| **Disposable staging branch** — bulk mutations write to a random temporary branch; its tree is folded into the final REST commit and the ref is deleted | The target branch still receives exactly one commit |
| **Local git SHA-1 diffing** — the SHA git would assign is computed locally and compared with the branch's tree | Unchanged files cost zero requests |
| **Content deduplication** — identical bytes upload once; blobs already in the repo are referenced by SHA | Repeated assets are free |
| **Pooled fallback uploads** — small binary sets and GraphQL-incompatible servers use parallel blob writes | Fast for small sends; compatibility is retained |
| **Proactive rate-limit pacing** with retry/backoff and `Retry-After` support | Avoids secondary-limit stalls and survives transient failures |
| **Parallel ZIP decompression** and connection reuse | Extraction and hashing overlap with I/O |

REST tree `content` is UTF-8-only, which used to leave binary-heavy archives on the
one-POST-per-file path. The bulk GraphQL writer removes that bottleneck: a 3,000-binary
send uses 30 content mutations rather than 3,000 blob POSTs.

## Real-time progress and request logging

Both the CLI and the browser page report progress live while the deploy runs:

* **Live progress bar** — percentage, current phase (extracting, bulk-staging,
  writing the tree, verifying, committing), files done, elapsed time, a live
  API-request counter, and an ETA. The CLI renders it on one terminal line
  (TTY only; `--no-progress` disables it); the browser shows the same data
  under the bar as `elapsed · requests · ETA`.
* **Request log** — every API round trip can be streamed into the log as
  `req #12 POST /git/trees -> 201 in 1094 ms | 194.3 KB sent`. Enable it with
  `--log-requests` (or `-v`) in the CLI; in the browser it is the
  "Log every API request" checkbox (on by default, and the log window trims
  itself so huge sends stay smooth).

```bash
python3 app.py --zip site.zip --owner you --repo site --log-requests
```

A 4,200-file archive (3,800 TypeScript sources + 400 binary assets) deployed
through the latency-simulating mock with the real 900-points/minute limit
enforced finishes in **18.2 seconds using 26 API requests**, verified
byte-for-byte — comfortably inside a 4-minute budget.

## Measured results

From `tests/benchmark.py`, which runs both implementations against a mock GitHub API that
simulates realistic latency **and enforces the real 900-points/minute secondary limit**.
The "original" rows run the pre-optimisation `app.py` verbatim, pulled out of git history.

```
scenario                                  files       time   requests     ok
------------------------------------------------------------------------------
Mixed web project, first deploy             3000      13.5s         21    yes
All-binary project, first deploy            3000      27.7s         49    yes
All-binary, no-op redeploy                  3000       1.9s          3    yes
All-binary, 1 file changed                  3000       4.7s          8    yes
Original, rate limit OFF (400 files)         400      13.9s        407    yes
Original, projected to 3000 (no limit)      3000    1.7 min       3005      -
Original, floor imposed by rate limit       3000   16.7 min       3005      -
Original, rate limit ON (400 files)          400       5.9s        184     NO
```

Two things worth reading twice:

* The original implementation **fails outright** once the real rate limit is enforced —
  it has no retry logic, so the first `403` aborts the deploy mid-way, leaving the branch
  untouched and the work wasted.
* The new engine's mixed and all-binary 3,000-file deploys were verified **byte for byte**
  (3000/3000) against locally computed git SHAs. The binary run made zero per-file
  `POST /git/blobs` calls and hit zero secondary-rate-limit rejections.

Run the binary benchmark yourself:

```bash
python3 tests/benchmark.py --profile binary --files 3000 --skip-legacy
```

### TypeScript / Astro projects

Source-heavy repositories are the best case, because almost everything is text and
therefore inlines. Benchmarked with `--profile astro`, whose fixture is generated to match
a real Astro site's language breakdown (TypeScript 92.2%, CSS 4.9%, Astro 2.3%, other
0.6%) and includes an oversized generated `.ts` bundle plus binary assets under `public/`:

```
scenario                                  files       time   requests     ok
------------------------------------------------------------------------------
New engine, first deploy                   3004      12.6s         20    yes
New engine, no-op redeploy                 3004       1.9s          3    yes
New engine, 1 file changed                 3004       4.5s          7    yes
Original, rate limit ON (400 files)         400       6.0s        184     NO
```

**3,004 files in 12.6 seconds using 20 requests**, all verified byte-exact — the 15
binary assets were bulk-staged while the other 2,989 files rode inline.

Run it yourself:

```bash
python3 tests/benchmark.py --profile astro --files 3000
```

#### A unicode gotcha this shook out

Python's `json.dumps` escapes non-ASCII by default, expanding every CJK character or emoji
into a 6-byte `\uXXXX` sequence, and the chunker was sizing payloads by *character* count
while UTF-8 spends 3–4 bytes on those same characters. On a Japanese or emoji-heavy repo
both errors compound. Measured on 3.1 MB of CJK content:

| | largest request body |
|---|---|
| before | 6.84 MB |
| after (UTF-8 body, byte-accurate sizing) | **2.79 MB** |

Both the Python and browser versions now send compact UTF-8 and budget chunks by the
real JSON byte length. If GitHub still rejects a tree POST as too large, the chunk is
split and retried so the send finishes instead of crashing. `tests/test_deploy.py` and
`tests/test_browser.mjs` assert the bound.

Numbers are latency-simulated rather than measured against github.com; what they capture
is the shape of the work — how many round trips, and whether the rate limit is tripped —
which is what governs real-world wall time.

## Optimised implementations

| Implementation | Status |
|---|---|
| [`app.py`](app.py) (Python) | Fast engine — inline text, batched binary GraphQL, SHA diffing, verification |
| [`index.html`](index.html) (browser) | Fast engine — same binary-safe algorithm in the browser |
| The other 9 language ports | Still the original one-blob-per-file approach; fine for small archives, subject to the 16.7-minute floor for large ones |

Porting the fast engine to the remaining languages is a good contribution — the Python
version is the reference implementation, and `tests/mock_github.py` will validate any port.

## Quick start

### Browser

Open [`index.html`](index.html) — no install, no server. Paste a token, pick a repo, drop
a ZIP. The token stays in the page and is never stored or transmitted anywhere except
api.github.com.

### Python

```bash
python app.py                                   # interactive prompts
```

Or non-interactively, which is what you want in CI:

```bash
export GITHUB_TOKEN=ghp_...
python app.py --owner octocat --repo hello-world --zip ./site.zip
```

No third-party packages are required. `requests` is used automatically for connection
pooling when it happens to be installed; otherwise the standard library handles it.

#### Options

| Flag | Purpose |
|---|---|
| `--owner`, `--repo`, `--branch`, `--zip` | Target and source (prompted if omitted) |
| `--token` | Token; prefer `$GITHUB_TOKEN` so it stays out of shell history |
| `--message`, `-m` | Commit message |
| `--strip-root` | Drop the single wrapper folder many ZIPs add |
| `--prune` | Delete repository files absent from the ZIP (mirror the archive) |
| `--dry-run` | Analyse the archive and estimate requests without writing |
| `--json` | Machine-readable summary on stdout |
| `--workers` | Parallel connections for ZIP extraction and fallback blob uploads (default 12) |
| `--no-inline` | Disable inline tree content |
| `--no-bulk` | Disable batched GraphQL binary writes and use per-blob fallback uploads |
| `--no-verify` | Skip the post-upload verification read |
| `--api-base`, `--graphql-base` | Point at GitHub Enterprise or a test server |
| `--quiet`, `-q` | Only warnings and errors |

Example output:

```
$ python app.py --owner octocat --repo hello-world --zip site.zip

GitHub ZIP Deployer — fast edition

[14:32:15] - Reading site.zip (12.4 MB)...
[14:32:16] - Extracted and hashed 3000 file(s), 41.2 MB in 0.71s
[14:32:16] - Target: octocat/hello-world on branch 'main'
[14:32:16] - Resolving branch 'main'...
[14:32:17] - Reading existing tree to skip unchanged files...
[14:32:17] - Plan: 2806 inlined into tree, 194 uploaded as blobs, 0 reused, 0 unchanged.
[14:32:18] - Bulk-staging 194 binary/large blob(s) in 2 GraphQL request(s)...
[14:32:19] - Bulk-staged 194 blob(s) in 2 request(s).
[14:32:19] - Writing 3000 tree entries in 8 request(s)...
[14:32:26] - Verifying tree contents against locally computed SHAs...
[14:32:29] + Verification passed — every file matches byte for byte.
[14:32:30] + Deployed 3000 file(s) in 13.5s (222 files/s) using 21 API requests.
```

## How it works

```mermaid
flowchart TD
    A[Read ZIP] --> B[Extract + hash in parallel<br/>local git SHA-1 per file]
    B --> C[Resolve branch<br/>init repo if empty]
    C --> D[Read existing tree recursively]
    D --> E{File SHA already<br/>at that path?}
    E -->|Yes| F[Skip: zero requests]
    E -->|No| G{Valid UTF-8?}
    G -->|Yes| H[Inline into tree entry]
    G -->|No, many| I[Bulk base64 additions<br/>GraphQL staging branch]
    G -->|No, few| P[Pooled blob fallback]
    H --> J[POST /git/trees in chunks<br/>chained onto staged tree]
    I --> J
    P --> J
    J --> K[Verify tree against local SHAs]
    K -->|Mismatch| L[Repair via blob upload]
    L --> K
    K -->|Clean| M[Create one target commit]
    M --> N[Update target ref<br/>delete staging ref]
```

1. **Read and hash.** Members are decompressed across a thread pool. `__MACOSX`,
   `.DS_Store`, `Thumbs.db`, `.git/` and any `..` traversal are dropped. The executable
   bit is preserved. Each file gets the SHA-1 git itself would assign.
2. **Diff.** The branch tree is read once; files whose path and SHA already match are
   skipped entirely.
3. **Split.** TypeScript, CSS, Astro and other valid UTF-8 ride inline — even a
   generated file of a couple of MB. Binary/oversized files are deduplicated by SHA.
4. **Bulk binary write.** Eight or more new binary blobs are base64-packed into bounded
   GraphQL batches (up to 100 files each) on a random temporary branch. Smaller sets use
   the pooled REST fallback. If GraphQL is unavailable, the fallback is automatic.
5. **Write.** Inline text, binary SHA references, deletions, and executable-mode fixes are
   written in bounded tree chunks chained onto the staged binary tree.
6. **Verify.** The resulting tree is read back and compared against locally computed SHAs.
   Any mismatch is repaired before the target commit is created.
7. **Commit** and update the target ref once, then delete the staging ref. The target branch
   receives one atomic commit and is never left half-deployed.

## Testing

```bash
./tests/run_all.sh            # everything, quick benchmark
./tests/run_all.sh --full     # everything, full 3000-file benchmark
```

Individually:

```bash
python3 tests/test_deploy.py               # Python correctness (31 tests)
node tests/test_browser.mjs                # browser end-to-end (needs: npm i --no-save jszip)
python3 tests/benchmark.py                 # head-to-head benchmark (generic web project)
python3 tests/benchmark.py --profile astro  # benchmark an Astro/TypeScript corpus
python3 tests/benchmark.py --profile binary # benchmark 100% binary assets
python3 tests/mock_github.py                # run the mock API standalone on :8080
```

[`tests/mock_github.py`](tests/mock_github.py) is a deliberately strict stand-in for
GitHub's Git Data API:

* blob SHAs are **real git SHA-1s**, so any corruption of content — encoding, newlines,
  truncation — changes the SHA and fails the test;
* tree entries must supply exactly one of `sha` or `content`, and an unknown `sha` is
  rejected with 422, as the real API does;
* the 900-points/minute secondary limit is enforced, with `403` + `Retry-After`;
* `createCommitOnBranch` is modeled with strict base64 decoding, binary bytes,
  expected-head checks, temporary refs, and real git blob SHAs;
* latency is simulated, including a per-entry cost for large tree and GraphQL writes, so
  a request carrying hundreds of files is not pretended to be free;
* faults can be injected (every Nth request returns a transient 500) to exercise retries.

The browser tests run the script **extracted from `index.html` itself** in a Node VM with
a stubbed DOM and `fetch` aimed at the mock, so the shipped code is what gets tested.

Coverage includes byte-exact round trips for unicode, CRLF, empty files, files containing
NUL bytes, binaries and executables; incremental redeploys; deduplication; pruning; path
filtering; empty-repository bootstrap; flaky-API retries; request-payload bounds on
CJK-heavy content; text files above the inline threshold; 3,000-file binary batching;
adaptive GraphQL payload splitting; temporary-ref cleanup; executable-mode correction;
and the SHA-1 fallback used when `crypto.subtle` is unavailable.

The optional benchmark dependencies (`requests`, `colorama` for running the original
implementation, `jszip` for the browser tests) are skipped with a clear message when
absent — the deployer itself needs none of them.

## Other language versions

| Language | File | Dependencies | Run Command |
|----------|------|--------------|--------------|
| Python | app.py | none (optional: requests, colorama) | `python app.py` |
| Go | app.go | (none, uses stdlib) | `go run app.go` |
| C++ | app.cpp | libcurl, libzip, nlohmann/json | `g++ -o app app.cpp -lcurl -lzip && ./app` |
| Erlang | app.erl | jsx, ibrowse (or httpc) | `escript app.erl` |
| Ruby | app.rb | rubyzip, json | `ruby app.rb` |
| PHP | index.php | zip, curl (extensions) | `php index.php` |
| Lua | app.lua | luasocket, lua-zip, json-lua | `lua app.lua` |
| Io | app.io | Io with the Zip addon | `io app.io` |
| Rebol | app.reb | Rebol 2 or 3 with Base64 | `rebol app.reb` |
| Haskell | app.hs | stack with aeson, zip-archive, http-conduit | `stack app.hs` |
| Forth | app.fs | Gforth, curl, jq, unzip | `gforth app.fs` |

These still use the original one-request-per-file algorithm. They work well for small
archives; for large ones, use the Python or browser version.

## Prerequisites

- A GitHub account.
- A personal access token with the `repo` scope
  ([create one](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)).
- Python 3.8+ for `app.py`, or any modern browser for `index.html`.

## Troubleshooting

* **Authentication failed** — check the token has `repo` scope and the owner/repo names
  are right. Fine-grained tokens need "Contents: write" on the target repository.
* **Secondary rate limit** — the Python and browser versions pace themselves under the
  budget and back off automatically; you should not see this. If you do, lower
  `--workers` or `--points-per-min`.
* **Temporary staging branches / CI** — binary batching creates and removes refs named
  `zip-deployer/stage-*`. A workflow configured for pushes to every branch may observe
  those short-lived staging commits. Ignore that prefix in the workflow, or use
  `--no-bulk` / uncheck **Batch binary files** to use the slower per-blob fallback.
* **Branch rules reject staging refs** — the deployer automatically falls back to pooled
  blob uploads. Allow the `zip-deployer/stage-*` prefix for full binary speed.
* **Branch not found / empty repository** — handled automatically: an initial commit with
  a `README.md` is created, then the deploy proceeds.
* **A file is over 100 MB** — GitHub's blob API rejects it. The deploy stops before
  writing anything and names the offending files. Use Git LFS for those.
* **Large archives** — the tools hold the archive and its extracted contents in memory.
  Budget roughly 2–3x the uncompressed size.
* **Verification failed** — the deploy aborts *before* creating the commit, so the branch
  is untouched. Please open an issue with the reported paths.

## Contributing

Contributions are welcome. Good next steps:

* Port the fast engine to the remaining languages (`tests/mock_github.py` validates ports).
* Resumable deploys for very large archives.
* A GitHub Action wrapper.

## License

This project is licensed under the MIT License. See [LICENSE.md](LICENSE.md).

> **Disclaimer** — provided "as is", without warranty of any kind. Use at your own risk,
> and keep backups before bulk operations. `--prune` deletes files.
