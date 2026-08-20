/**
 * End-to-end test for the browser deployer.
 *
 * The script inside index.html is extracted and executed verbatim in a Node VM
 * with a minimal DOM, the real JSZip, and `fetch` pointed at the same strict
 * mock GitHub API the Python tests use. So this exercises the shipped browser
 * code — hashing, inlining, blob uploads, batching, verification — rather than
 * a copy of it.
 *
 *   node tests/test_browser.mjs
 */

import { readFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const JSZip = require('jszip');

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = dirname(here);
const PORT = 8931 + Math.floor(Math.random() * 400);
const MOCK_URL = `http://127.0.0.1:${PORT}`;

let failures = 0;
let checks = 0;

function check(condition, label) {
    checks++;
    if (condition) {
        console.log(`  ok   ${label}`);
    } else {
        failures++;
        console.log(`  FAIL ${label}`);
    }
}

// ---------------------------------------------------------------------------
// Mock server lifecycle
// ---------------------------------------------------------------------------

async function startMock() {
    const proc = spawn('python3', [
        join(here, 'mock_github.py'),
        '--host', '127.0.0.1',
        '--port', String(PORT),
        '--latency', '0',
        '--write-latency', '0',
        '--points-per-min', '0'
    ], { stdio: ['ignore', 'pipe', 'pipe'] });

    for (let i = 0; i < 100; i++) {
        try {
            await fetch(`${MOCK_URL}/repos/x/y/_debug/files/main`);
            return proc;
        } catch {
            await new Promise((r) => setTimeout(r, 100));
        }
    }
    throw new Error('mock server did not start');
}

async function remoteStats(owner, repo) {
    const res = await fetch(`${MOCK_URL}/repos/${owner}/${repo}/_debug/stats`);
    return res.json();
}

async function remoteDebug(owner, repo, branch = 'main') {
    const res = await fetch(`${MOCK_URL}/repos/${owner}/${repo}/_debug/files/${branch}`);
    return res.json();
}

async function remoteFiles(owner, repo, branch = 'main') {
    const payload = await remoteDebug(owner, repo, branch);
    const out = new Map();
    for (const [path, meta] of Object.entries(payload.files)) {
        out.set(path, { bytes: Buffer.from(meta.b64, 'base64'), mode: meta.mode });
    }
    return out;
}

// ---------------------------------------------------------------------------
// Browser environment
// ---------------------------------------------------------------------------

function extractScript() {
    const html = readFileSync(join(repoRoot, 'index.html'), 'utf8');
    const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)];
    if (!blocks.length) throw new Error('no inline script found in index.html');
    return blocks[blocks.length - 1][1];
}

function makeElement(id) {
    const listeners = {};
    const el = {
        id,
        value: '',
        checked: false,
        disabled: false,
        textContent: '',
        innerHTML: '',
        files: [],
        style: {},
        listeners,
        classList: { add() {}, remove() {}, contains: () => false },
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
        dispatch(name, event) { return Promise.all((listeners[name] || []).map((fn) => fn(event))); },
        insertAdjacentHTML(position, html) {
            if (position === 'beforeend') el.innerHTML += html;
            else if (position === 'afterbegin') el.innerHTML = html + el.innerHTML;
            else el.innerHTML += html;
        }
    };
    return el;
}

function makeSandbox({ withSubtle = true } = {}) {
    const elements = new Map();
    const getElement = (id) => {
        if (!elements.has(id)) elements.set(id, makeElement(id));
        return elements.get(id);
    };

    // Defaults matching the markup
    getElement('branch').value = 'main';
    getElement('opt-skip').checked = true;
    getElement('opt-verify').checked = true;
    getElement('opt-strip').checked = false;
    getElement('opt-prune').checked = false;
    getElement('opt-bulk').checked = true;

    const alerts = [];
    const sandbox = {
        console,
        setTimeout,
        clearTimeout,
        Date,
        Math,
        TextEncoder,
        TextDecoder,
        btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
        atob: (s) => Buffer.from(s, 'base64').toString('binary'),
        JSZip,
        crypto: withSubtle ? globalThis.crypto : { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) },
        localStorage: { getItem: () => null, setItem: () => {} },
        alert: (msg) => alerts.push(msg),
        lucide: { createIcons() {} },
        document: {
            getElementById: getElement,
            createElement: () => makeElement('created')
        },
        // Route the hard-coded api.github.com host at the mock server.
        fetch: (url, options) => fetch(String(url).replace('https://api.github.com', MOCK_URL), options)
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    return { sandbox, getElement, alerts };
}

async function runDeploy({ zipBuffer, owner, repo, options = {}, withSubtle = true }) {
    const { sandbox, getElement } = makeSandbox({ withSubtle });
    vm.runInNewContext(extractScript(), vm.createContext(sandbox), { filename: 'index.html' });

    getElement('pat').value = 'test-token';
    getElement('owner').value = owner;
    getElement('repo').value = repo;
    getElement('zip-file').files = [zipBuffer];
    for (const [key, value] of Object.entries(options)) getElement(key).checked = value;

    await getElement('upload-btn').dispatch('click');
    return {
        log: getElement('log').innerHTML,
        summary: getElement('summary').innerHTML
    };
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

async function buildZip(files) {
    const zip = new JSZip();
    for (const [path, data] of Object.entries(files)) zip.file(path, data);
    return zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
}

const TRICKY = {
    'README.md': Buffer.from('# Hello\n\nPlain ascii.\n'),
    'src/app.js': Buffer.from("console.log('hi');\n"),
    'src/nested/deep/config.json': Buffer.from('{\n  "a": 1\n}\n'),
    'docs/unicode.md': Buffer.from('# Ünïcödé — emoji 🚀 and CJK 日本語\n', 'utf8'),
    'docs/crlf.txt': Buffer.from('line one\r\nline two\r\n'),
    'docs/no-newline.txt': Buffer.from('no newline at end'),
    'docs/empty.txt': Buffer.alloc(0),
    'assets/logo.png': Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from(Array.from({ length: 512 }, (_, i) => i % 256))]),
    'assets/nulls.bin': Buffer.from('has\u0000null\u0000bytes', 'binary'),
    'weird name (1).txt': Buffer.from('spaces and parens\n')
};

const JUNK = {
    'real.txt': Buffer.from('real\n'),
    '__MACOSX/._real.txt': Buffer.from('junk'),
    'folder/.DS_Store': Buffer.from('junk'),
    '.git/config': Buffer.from('[core]\n')
};

// ---------------------------------------------------------------------------

async function main() {
    const mock = await startMock();
    try {
        // --- 1. byte-exact round trip -------------------------------------
        console.log('\nbrowser: content fidelity');
        const trickyZip = await buildZip(TRICKY);
        const run1 = await runDeploy({ zipBuffer: trickyZip, owner: 'octocat', repo: 'fidelity' });
        const deployed = await remoteFiles('octocat', 'fidelity');
        let allMatch = true;
        for (const [path, expected] of Object.entries(TRICKY)) {
            const got = deployed.get(path);
            if (!got || !got.bytes.equals(expected)) {
                allMatch = false;
                console.log(`       mismatch: ${path} (${got ? got.bytes.length : 'missing'} vs ${expected.length} bytes)`);
            }
        }
        check(allMatch, 'every file round-trips byte for byte');
        check(run1.log.includes('Verification passed'), 'verification step ran and passed');
        check(!run1.log.includes('Deployment failed'), 'no errors logged');

        // --- 2. batching --------------------------------------------------
        console.log('\nbrowser: request batching at scale');
        const many = {};
        for (let i = 0; i < 1000; i++) many[`src/mod${String(i).padStart(4, '0')}.js`] = Buffer.from(`export const n = ${i};\n`);
        const manyZip = await buildZip(many);
        const run2 = await runDeploy({ zipBuffer: manyZip, owner: 'octocat', repo: 'batching' });
        const batched = await remoteFiles('octocat', 'batching');
        const requestMatch = run2.log.match(/using (\d+) API requests/);
        const requests = requestMatch ? Number(requestMatch[1]) : Infinity;
        check(batched.size >= 1000, `all 1000 files deployed (got ${batched.size - 1})`);
        check(requests < 30, `1000 files cost ${requests} API requests, not ~1000`);

        // --- parallel binary staging ---------------------------------------
        console.log('\nbrowser: parallel binary staging');
        const music = {};
        for (let i = 0; i < 60; i++) {
            const data = Buffer.alloc(256 * 1024, i % 251);
            Buffer.from(`PNG-${String(i).padStart(4, '0')}\0`).copy(data);
            music[`music/cover${String(i).padStart(4, '0')}.png`] = data;
        }
        const musicRun = await runDeploy({
            zipBuffer: await buildZip(music), owner: 'octocat', repo: 'music'
        });
        const musicFiles = await remoteFiles('octocat', 'music');
        const musicStats = await remoteStats('octocat', 'music');
        const musicDebug = await remoteDebug('octocat', 'music');
        let musicExact = true;
        for (const [path, expected] of Object.entries(music)) {
            if (!musicFiles.get(path)?.bytes.equals(expected)) musicExact = false;
        }
        check(musicExact, 'parallel-staged binaries are byte exact');
        check((musicStats.by_endpoint['POST /graphql'] || 0) >= 5,
            'work was distributed over multiple GraphQL chains');
        check(musicStats.refs.length === 1 && musicStats.refs[0] === 'main',
            'all disposable staging refs were removed');
        check(musicDebug.parents.length === 1, 'target branch received one final deploy commit');
        check(/MB/.test(musicRun.log), 'binary progress reports uploaded MB');

        // --- 3. incremental redeploy ---------------------------------------
        console.log('\nbrowser: incremental redeploy');
        const run3 = await runDeploy({ zipBuffer: manyZip, owner: 'octocat', repo: 'batching' });
        check(run3.log.includes('already up to date'), 'unchanged redeploy is a no-op');

        const touched = { ...many, 'src/mod0000.js': Buffer.from('// changed\n') };
        const run4 = await runDeploy({ zipBuffer: await buildZip(touched), owner: 'octocat', repo: 'batching' });
        const after = await remoteFiles('octocat', 'batching');
        check(run4.log.includes('999 file(s) already identical'), 'only the changed file is re-sent');
        check(after.get('src/mod0000.js').bytes.toString() === '// changed\n', 'changed file has new content');

        // --- 4. junk filtering ---------------------------------------------
        console.log('\nbrowser: path filtering');
        await runDeploy({ zipBuffer: await buildZip(JUNK), owner: 'octocat', repo: 'junk' });
        const junkFiles = await remoteFiles('octocat', 'junk');
        junkFiles.delete('README.md');
        check(junkFiles.size === 1 && junkFiles.has('real.txt'), 'macOS/.git junk is filtered out');

        // --- 5. prune --------------------------------------------------------
        console.log('\nbrowser: prune');
        await runDeploy({ zipBuffer: await buildZip({ 'keep.txt': Buffer.from('k'), 'drop.txt': Buffer.from('d') }), owner: 'octocat', repo: 'prune' });
        await runDeploy({
            zipBuffer: await buildZip({ 'keep.txt': Buffer.from('k') }),
            owner: 'octocat', repo: 'prune', options: { 'opt-prune': true }
        });
        const pruned = await remoteFiles('octocat', 'prune');
        check(!pruned.has('drop.txt') && pruned.has('keep.txt'), 'prune deletes files missing from the ZIP');

        // --- 6. payload bounds with CJK content --------------------------------
        console.log('\nbrowser: payload bounds with CJK/emoji content');
        const cjkBody = '日本語のページ 🚀 Ünïcödé\n'.repeat(200);
        const cjk = {};
        for (let i = 0; i < 400; i++) cjk[`src/pages/page${String(i).padStart(4, '0')}.astro`] = Buffer.from(cjkBody + `// ${i}\n`, 'utf8');
        await runDeploy({ zipBuffer: await buildZip(cjk), owner: 'octocat', repo: 'cjk' });
        const cjkStats = await remoteStats('octocat', 'cjk');
        const cjkFiles = await remoteFiles('octocat', 'cjk');
        let cjkExact = true;
        for (const [path, expected] of Object.entries(cjk)) {
            const got = cjkFiles.get(path);
            if (!got || !got.bytes.equals(expected)) cjkExact = false;
        }
        check(cjkExact, 'CJK/emoji content round-trips byte for byte');
        check(cjkStats.max_request_bytes < 6 * 1024 * 1024,
            `largest request body stays bounded (${(cjkStats.max_request_bytes / 1e6).toFixed(2)} MB)`);

        // --- 7. SHA-1 fallback path -------------------------------------------
        console.log('\nbrowser: SHA-1 fallback (no crypto.subtle)');
        const run6 = await runDeploy({ zipBuffer: trickyZip, owner: 'octocat', repo: 'fallback', withSubtle: false });
        const fallbackFiles = await remoteFiles('octocat', 'fallback');
        let fallbackMatch = true;
        for (const [path, expected] of Object.entries(TRICKY)) {
            const got = fallbackFiles.get(path);
            if (!got || !got.bytes.equals(expected)) fallbackMatch = false;
        }
        check(run6.log.includes('built-in SHA-1'), 'falls back when crypto.subtle is missing');
        check(fallbackMatch, 'fallback SHA-1 produces an identical deployment');
        check(run6.log.includes('Verification passed'), 'fallback path still verifies clean');

        // --- 8. binary assets must not poison later TypeScript inlining ------
        console.log('\nbrowser: binaries do not force later .ts files down the blob path');
        const mixed = {
            'public/img/photo.webp': Buffer.from([0x52, 0x49, 0x46, 0x46, 0xff, 0xfe, 0xfd, 0x00]),
            'public/img/logo.png': Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from([0xff, 0xfe])])
        };
        for (let i = 0; i < 200; i++) {
            mixed[`src/lib/mod${String(i).padStart(3, '0')}.ts`] = Buffer.from(`export const n${i} = ${i};\n`);
        }
        const run7 = await runDeploy({ zipBuffer: await buildZip(mixed), owner: 'octocat', repo: 'astrots' });
        const mixedFiles = await remoteFiles('octocat', 'astrots');
        const mixedStats = await remoteStats('octocat', 'astrots');
        const reqMatch = run7.log.match(/using (\d+) API requests/);
        const mixedRequests = reqMatch ? Number(reqMatch[1]) : Infinity;
        check(mixedFiles.has('src/lib/mod000.ts'), 'TypeScript files were deployed');
        check(mixedRequests < 25, `200 .ts + 2 binaries cost ${mixedRequests} requests, not ~200`);
        check(run7.log.includes('Verification passed'), 'mixed TS/binary send verified clean');
        check(mixedStats.max_request_bytes < 6 * 1024 * 1024, 'mixed send stayed inside the payload budget');

        // --- 9. binary-heavy archives use GraphQL batches --------------------
        console.log('\nbrowser: binary-heavy archives are bulk-staged');
        const binaries = {};
        for (let i = 0; i < 1000; i++) {
            const data = Buffer.alloc(128, i % 251);
            data[0] = 0;
            data.writeUInt32BE(i, 1);
            binaries[`assets/chunk${String(i).padStart(4, '0')}.bin`] = data;
        }
        const statsBeforeBinary = await remoteStats('octocat', 'binary-bulk');
        const run9 = await runDeploy({
            zipBuffer: await buildZip(binaries), owner: 'octocat', repo: 'binary-bulk'
        });
        const binaryFiles = await remoteFiles('octocat', 'binary-bulk');
        const binaryStats = await remoteStats('octocat', 'binary-bulk');
        const graphqlDelta = (binaryStats.by_endpoint['POST /graphql'] || 0) -
            (statsBeforeBinary.by_endpoint['POST /graphql'] || 0);
        const blobDelta = (binaryStats.by_endpoint['POST /git/blobs'] || 0) -
            (statsBeforeBinary.by_endpoint['POST /git/blobs'] || 0);
        const binaryReq = Number((run9.log.match(/using (\d+) API requests/) || [])[1] || Infinity);
        check(binaryFiles.size >= 1000, `all 1000 binary files deployed (got ${binaryFiles.size - 1})`);
        check(binaryReq < 60, `1000 binaries cost ${binaryReq} requests, not ~1000`);
        check(graphqlDelta === 10, '1000 binaries were packed into 10 GraphQL mutations');
        check(blobDelta === 12, '12-worker REST lane ran concurrently with bulk staging');
        check(binaryFiles.get('assets/chunk0999.bin')?.bytes.equals(binaries['assets/chunk0999.bin']),
            'bulk-staged binary content is byte-exact');
        check(run9.log.includes('Verification passed'), 'bulk binary send verified clean');

        // --- 10. generated TypeScript bundle stays on the fast path ----------
        console.log('\nbrowser: generated .ts bundle stays inline');
        const fat = {
            'src/generated/api-types.ts': Buffer.alloc(1_200_000, 97),
            'src/lib/ok.ts': Buffer.from('export const ok = 1;\n')
        };
        const run8 = await runDeploy({ zipBuffer: await buildZip(fat), owner: 'octocat', repo: 'fatties' });
        const fatFiles = await remoteFiles('octocat', 'fatties');
        const fatReq = Number((run8.log.match(/using (\d+) API requests/) || [])[1] || Infinity);
        check(fatFiles.get('src/generated/api-types.ts')?.bytes.length === 1_200_000, '1.2 MB .ts round-tripped');
        check(fatReq < 15, `1.2 MB .ts + 1 small file cost ${fatReq} requests, not a blob POST each`);
        check(run8.log.includes('Verification passed'), 'large .ts send verified clean');

        console.log(`\n${checks - failures}/${checks} checks passed`);
        return failures === 0 ? 0 : 1;
    } finally {
        mock.kill('SIGTERM');
    }
}

main().then((code) => process.exit(code)).catch((error) => {
    console.error(error);
    process.exit(1);
});
