import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, statfs } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const lock = JSON.parse(await readFile(path.join(root, 'resources/sources.lock.json'), 'utf8'));
const mode = process.argv[2] ?? 'verify';
const allowMissing = process.argv.includes('--allow-missing');
const report = { checkedAt: new Date().toISOString(), mode, projects: [] };
const sha = (algorithm, bytes) => createHash(algorithm).update(bytes).digest('hex');
const blobHash = bytes => createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
const total = lock.projects.reduce((s, p) => s + p.files.reduce((n, f) => n + f.bytes, 0), 0);
if (total > lock.policy.maxTotalBytes) throw new Error('Combined reference budget exceeded; nothing downloaded.');
for (const project of lock.projects) {
  if (!/^[\w-]+\/[\w.-]+$/.test(project.repository) || !/^[a-f0-9]{40}$/.test(project.commit)) throw new Error('Invalid source identity');
  const bytes = project.files.reduce((n, f) => n + f.bytes, 0);
  if (bytes > lock.policy.maxProjectBytes) throw new Error(`${project.id}: source budget exceeded`);
  const result = { id: project.id, commit: project.commit, expectedFiles: project.files.length, expectedBytes: bytes, verified: [], missing: [], errors: [] };
  let cursor = 0;
  async function worker() {
    while (cursor < project.files.length) {
      const file = project.files[cursor++];
      if (file.path.split('/').some(p => !p || p === '..') || file.path.includes('\\') || path.isAbsolute(file.path) || file.bytes > lock.policy.maxFileBytes) throw new Error('Unsafe path or oversized file');
      const target = path.join(root, 'resources/upstream', project.id, file.path);
      let data, downloaded = false;
      try { data = await readFile(target); }
      catch (e) {
        if (e.code !== 'ENOENT') throw e;
        if (mode !== 'download') { result.missing.push(file.path); continue; }
        const disk = await statfs(root);
        if (os.freemem() < lock.policy.minFreeMemoryBytes || disk.bavail * disk.bsize < lock.policy.minFreeDiskBytes) {
          result.errors.push({ path: file.path, reason: 'Resource guard: less than 512 MiB available RAM or 30 GiB free disk; deferred.' });
          continue;
        }
        const url = `https://raw.githubusercontent.com/${project.repository}/${project.commit}/${file.path}`;
        try {
          for (let attempt = 0; attempt < 3; attempt++) {
            try {
              const response = await fetch(url, { signal: AbortSignal.timeout(20000) });
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              const chunks = []; let received = 0;
              for await (const chunk of response.body) {
                received += chunk.length;
                if (received > file.bytes || received > lock.policy.maxFileBytes) throw new Error('File exceeds declared budget');
                chunks.push(chunk);
              }
              data = Buffer.concat(chunks); downloaded = true; break;
            } catch (error) { if (attempt === 2) throw error; }
          }
        } catch (e) { result.errors.push({ path: file.path, reason: e.message }); continue; }
      }
      if (data.length !== file.bytes || blobHash(data) !== file.gitBlob) {
        result.errors.push({ path: file.path, reason: 'Size or Git blob hash mismatch' }); continue;
      }
      if (downloaded) { await mkdir(path.dirname(target), { recursive: true }); await writeFile(target, data); }
      result.verified.push({ path: file.path, bytes: data.length, gitBlob: file.gitBlob, sha256: sha('sha256', data) });
      if (result.verified.length % 50 === 0) console.log(`${project.id}: ${result.verified.length}/${project.files.length}`);
    }
  }
  await Promise.all(Array.from({ length: lock.policy.concurrency }, worker));
  result.verified.sort((a, b) => a.path.localeCompare(b.path));
  report.projects.push(result);
  console.log(`${project.id}: verified ${result.verified.length}/${project.files.length}, missing ${result.missing.length}, errors ${result.errors.length}`);
}
await mkdir(path.join(root, '.cache'), { recursive: true });
await writeFile(path.join(root, '.cache/references-report.json'), JSON.stringify(report, null, 2) + '\n');
const errors = report.projects.flatMap(p => p.errors);
if (errors.length) console.log(JSON.stringify(errors.slice(0, 4), null, 2));
if (errors.length || (!allowMissing && report.projects.some(p => p.missing.length))) process.exitCode = 1;
