import { execFileSync } from 'node:child_process';
import { readdir, readFile } from 'node:fs/promises';
const files = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean);
const bad = files.filter(p => /^(legacy|resources\/upstream|node_modules|dist|\.cache)\//.test(p) || /(^|\/)\.env(?:\.|$)/.test(p));
if (bad.length) throw new Error(`Local-only files in Git index: ${bad.join(', ')}`);
async function scan(dir) {
  for (const item of await readdir(dir, { withFileTypes: true })) {
    const p = `${dir}/${item.name}`;
    if (item.isDirectory()) await scan(p);
    else if (/\.[cm]?[jt]sx?$/.test(p)) {
      const text = await readFile(p, 'utf8');
      if (/(?:from\s*|import\s*\(|require\s*\()\s*['"][^'"]*(?:legacy|resources\/upstream)\//.test(text)) throw new Error(`Forbidden runtime import: ${p}`);
    }
  }
}
await scan('src');
console.log('Source and Git boundaries PASS.');
