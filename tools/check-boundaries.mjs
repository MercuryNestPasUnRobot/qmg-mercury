import { execFileSync } from 'node:child_process';
import { readdir, readFile } from 'node:fs/promises';
const files = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean);
const bad = files.filter(p => /^(legacy|local-content|resources\/upstream|node_modules|dist|\.cache|\.runtime)\//.test(p) || /(^|\/)\.venv\//.test(p) || /(^|\/)\.env(?:\.|$)/.test(p));
if (bad.length) throw new Error(`Local-only files in Git index: ${bad.join(', ')}`);
async function scan(dir) {
  for (const item of await readdir(dir, { withFileTypes: true })) {
    const p = `${dir}/${item.name}`;
    if (item.isDirectory()) await scan(p);
    else if (/\.[cm]?[jt]sx?$|\.py$/.test(p)) {
      const text = await readFile(p, 'utf8');
      if (/(?:from\s*|import\s*\(|require\s*\()\s*['"][^'"]*(?:legacy|resources\/upstream)\//.test(text)) throw new Error(`Forbidden runtime import: ${p}`);
      if (/(?:from|import)\s+(?:legacy|resources\.upstream)\b/.test(text)) throw new Error(`Forbidden Python import: ${p}`);
    }
  }
}
await scan('src');
await scan('core/qmg_core');
console.log('Source and Git boundaries PASS.');
