// One-time, byte-preserving relocation. Never run builds against the archive.
import { createReadStream } from 'node:fs';
import { mkdir, readdir, lstat, readlink, rename, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const archive = 'legacy/2026-11-19';
const names = ['.gitignore', '.npm-cache', '.training-runtime', 'assets', 'build', 'dist', 'docs', 'electron', 'former-project', 'Images', 'index.html', 'node_modules', 'package-lock.json', 'package.json', 'public', 'README.md', 'reference', 'release', 'replays.html', 'scripts', 'src', 'tmp', 'training', 'training-data', 'tsconfig.json', 'vite.config.ts'];
const excluded = ['.npm-cache', '.training-runtime', 'node_modules', 'dist', 'release', 'tmp', 'training/.venv'];
const files = [], directories = [], links = [];
const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
function inside(relative) {
  const absolute = path.resolve(root, relative);
  if (!absolute.startsWith(root + path.sep)) throw new Error(`Outside workspace: ${relative}`);
  return absolute;
}
function skip(p) {
  return excluded.some(x => p === x || p.startsWith(x + '/')) || p.split('/').includes('__pycache__');
}
async function hash(p) {
  const h = createHash('sha256');
  for await (const chunk of createReadStream(p, { highWaterMark: 256 * 1024 })) h.update(chunk);
  return h.digest('hex');
}
async function scan(p) {
  if (skip(p)) return;
  const abs = inside(p), stat = await lstat(abs);
  if (stat.isSymbolicLink()) { links.push({ path: p, target: await readlink(abs) }); return; }
  if (stat.isDirectory()) {
    directories.push(p);
    for (const name of (await readdir(abs)).sort()) await scan(`${p}/${name}`);
  } else files.push({ path: p, bytes: stat.size, sha256: await hash(abs) });
}
// Preflight every move before touching anything; refuse overwrites or reruns.
for (const name of names) {
  await lstat(inside(name));
  try { await lstat(inside(`${archive}/${name}`)); throw new Error(`Archive already exists: ${name}`); }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
}
const baseline = { branch: git('branch', '--show-current'), head: git('rev-parse', 'HEAD'), status: git('status', '--porcelain=v1'), remotes: git('remote', '-v') };
console.log('Hashing old source, documents, assets and training records (streaming)...');
for (const name of names) await scan(name);
const manifest = { schemaVersion: 1, capturedAt: new Date().toISOString(), archive, baseline, relocatedEntries: names, excludedFromHash: [...excluded, '**/__pycache__/**'], exclusionReason: 'Generated caches, dependencies, virtual environments and temporary runs are relocated unchanged but not individually hashed. No files are deleted.', directories, links, files };
await mkdir(inside('resources/legacy'), { recursive: true });
await writeFile(inside('resources/legacy/manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
await mkdir(inside(archive), { recursive: true });
for (const name of names) {
  await rename(inside(name), inside(`${archive}/${name}`));
  console.log(`Archived ${name}`);
}
console.log(JSON.stringify({ files: files.length, bytes: files.reduce((s, f) => s + f.bytes, 0), archive }));
