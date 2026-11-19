import { createReadStream } from 'node:fs';
import { readFile, readdir, lstat, readlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const manifest = JSON.parse(await readFile(path.join(root, 'resources/legacy/manifest.json'), 'utf8'));
const archive = path.join(root, manifest.archive);
try { await lstat(archive); }
catch (e) {
  if (e.code === 'ENOENT' && process.argv.includes('--allow-missing')) {
    console.log('Local legacy archive is absent (expected in a fresh public clone). Manifest retained; byte verification not performed.');
    process.exit(0);
  }
  throw e;
}
const errors = [], expected = new Set(manifest.files.map(f => f.path));
for (const file of manifest.files) {
  try {
    const target = path.resolve(archive, file.path);
    if (!target.startsWith(archive + path.sep)) throw new Error('Unsafe manifest path');
    const hash = createHash('sha256'); let bytes = 0;
    for await (const chunk of createReadStream(target, { highWaterMark: 256 * 1024 })) { bytes += chunk.length; hash.update(chunk); }
    if (bytes !== file.bytes || hash.digest('hex') !== file.sha256) errors.push(`Changed: ${file.path}`);
  } catch (e) { errors.push(`${file.path}: ${e.code ?? e.message}`); }
}
const skip = p => manifest.excludedFromHash.some(x => !x.includes('*') && (p === x || p.startsWith(x + '/'))) || p.split('/').includes('__pycache__');
async function checkExtras(p) {
  if (skip(p)) return;
  const s = await lstat(path.join(archive, p));
  if (s.isSymbolicLink()) {
    const expectedLink = manifest.links.find(x => x.path === p);
    if (!expectedLink || expectedLink.target !== await readlink(path.join(archive, p))) errors.push(`Changed link: ${p}`);
  } else if (s.isDirectory()) {
    for (const name of await readdir(path.join(archive, p))) await checkExtras(`${p}/${name}`);
  } else if (!expected.has(p)) errors.push(`Unexpected: ${p}`);
}
for (const name of manifest.relocatedEntries) await checkExtras(name);
if (errors.length) { console.error(errors.join('\n')); process.exitCode = 1; }
else console.log(`Legacy integrity PASS: ${manifest.files.length} files, ${manifest.files.reduce((s, f) => s + f.bytes, 0)} bytes unchanged.`);
