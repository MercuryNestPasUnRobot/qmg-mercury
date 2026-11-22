import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const local = path.join(root, 'core/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const base = process.env.QMG_PYTHON ?? (existsSync(path.join(root, '.runtime/python/python.exe')) ? path.join(root, '.runtime/python/python.exe') : process.platform === 'win32' ? 'python' : 'python3');
const mode = process.argv[2] ?? 'test';
function run(executable, args) {
  const result = spawnSync(executable, args, { cwd: path.join(root, 'core'), stdio: 'inherit', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' } });
  if (result.error) { console.error('Python 3.11+ required. Set QMG_PYTHON or run npm run core:setup.'); process.exit(1); }
  if (result.status !== 0) process.exit(result.status ?? 1);
}
if (mode === 'setup') {
  run(base, ['-m', 'venv', '.venv']);
  run(local, ['-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', '-r', 'requirements-dev.txt']);
} else if (mode === 'test') run(existsSync(local) ? local : base, ['-m', 'pytest', ...process.argv.slice(3)]);
else if (mode === 'demo') run(existsSync(local) ? local : base, ['-m', 'qmg_core']);
else if (mode === 'play') run(existsSync(local) ? local : base, ['-m', 'qmg_core.workbench', ...process.argv.slice(3)]);
else if (mode === 'data') run(existsSync(local) ? local : base, ['-m', 'qmg_core.catalog', ...process.argv.slice(3)]);
else if (mode === 'migrate') run(existsSync(local) ? local : base, ['-m', 'qmg_core.migration', ...process.argv.slice(3)]);
else if (mode === 'verify-content') run(existsSync(local) ? local : base, ['-m', 'qmg_core.content_verify', ...process.argv.slice(3)]);
else if (mode === 'audit-content') run(existsSync(local) ? local : base, ['-m', 'qmg_core.content_audit', ...process.argv.slice(3)]);
else if (mode === 'ai-trial') run(existsSync(local) ? local : base, ['-m', 'qmg_core.ai_trial', ...process.argv.slice(3)]);
else if (mode === 'ai-match') run(existsSync(local) ? local : base, ['-m', 'qmg_core.ai_match', ...process.argv.slice(3)]);
else throw new Error('Unknown core action');
