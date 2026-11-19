import { execFileSync } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
const version = process.versions.node.split('.').map(Number);
const nodeOK = version[0] === 24 && version[1] >= 12;
const getGit = (...args) => { try { return execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); } catch { return null; } };
let references = null;
try { references = JSON.parse(await readFile('.cache/references-report.json', 'utf8')).projects.map(p => ({ id: p.id, verified: p.verified.length, expected: p.expectedFiles, errors: p.errors.length })); } catch {}
const report = { checkedAt: new Date().toISOString(), node: process.versions.node, nodeOK, platform: process.platform, freeMemoryMiB: Math.round(os.freemem() / 2 ** 20), git: getGit('--version'), branch: getGit('branch', '--show-current'), origin: getGit('remote', 'get-url', 'origin'), references };
await mkdir('.cache', { recursive: true });
await writeFile('.cache/environment.json', JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report, null, 2));
if (!nodeOK || !report.git) process.exitCode = 1;
