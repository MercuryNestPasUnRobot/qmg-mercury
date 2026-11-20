import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import ts from 'typescript';

const workspace = path.resolve(import.meta.dirname, '..');
const frozen = path.join(workspace, 'legacy/2026-11-19');
const destination = path.join(workspace, 'local-content/legacy-2026-11-19');
const sources = [
  'src/engine/types.ts', 'src/engine/map.ts', 'src/engine/reviewed-cards.ts',
  'src/engine/content.ts', 'src/assets/generic-manifest.json',
  'assets/legacy-card-transcription.json', 'assets/card-worklist.json',
];

mkdirSync(destination, { recursive: true });
const provenance = [];
for (const relative of sources) {
  const bytes = readFileSync(path.join(frozen, relative));
  const copy = path.join(destination, 'source', relative);
  mkdirSync(path.dirname(copy), { recursive: true });
  if (existsSync(copy) && !readFileSync(copy).equals(bytes)) {
    throw new Error('Existing transcription copy differs from its frozen source: ' + relative);
  }
  if (!existsSync(copy)) writeFileSync(copy, bytes, { flag: 'wx' });
  provenance.push({
    source: 'legacy/2026-11-19/' + relative,
    copy: path.relative(workspace, copy).replaceAll(path.sep, '/'),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'),
  });
}

// Only copied data modules are evaluated. Their imports are confined to this allowlist.
const modules = new Map();
function evaluate(relative) {
  if (modules.has(relative)) return modules.get(relative);
  if (!sources.includes(relative)) throw new Error('Data module is outside the export allowlist: ' + relative);
  const source = readFileSync(path.join(destination, 'source', relative), 'utf8');
  if (relative.endsWith('.json')) return JSON.parse(source);
  const exports = {};
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const context = vm.createContext({
    exports,
    require(request) {
      const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(relative), request));
      return evaluate(resolved.endsWith('.json') ? resolved : resolved + '.ts');
    },
  });
  vm.runInContext(transpiled, context, { filename: relative, timeout: 5000 });
  modules.set(relative, exports);
  return exports;
}
const { starterPack, COUNTRY_NAMES, COUNTRY_SIDE } = evaluate('src/engine/content.ts');
const { reviewedCards } = evaluate('src/engine/reviewed-cards.ts');
const counts = Object.fromEntries(Object.entries(starterPack.decks).map(([country, cards]) => [country, cards.length]));
const output = {
  format: 'qmg-user-transcriptions-v1',
  pack: starterPack,
  country_names: COUNTRY_NAMES,
  country_teams: COUNTRY_SIDE,
  reviewed: reviewedCards.map(({ definition, ...source }) => ({ card_id: definition.id, ...source })),
};
writeFileSync(path.join(destination, 'content.json'), JSON.stringify(output, null, 2) + '\n');
writeFileSync(path.join(destination, 'provenance.json'), JSON.stringify({ sources: provenance }, null, 2) + '\n');
console.log(JSON.stringify({ destination, cards: starterPack.cards.length, copies: counts }));
