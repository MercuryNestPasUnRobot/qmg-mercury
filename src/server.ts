import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

export function createApp() {
  // Explicit routes only: never expose the workspace or the local archive.
  return createServer(async (request, response) => {
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'GET') {
      response.writeHead(405, { Allow: 'GET' }).end('Method not allowed');
      return;
    }
    const pathname = (request.url ?? '/').split('?')[0];
    if (pathname === '/health') {
      response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      response.end(JSON.stringify({ project: 'qmg-mercury', version: '0.1.0', status: 'foundation-ready' }));
      return;
    }
    if (pathname !== '/') {
      response.writeHead(404).end('Not found');
      return;
    }
    try {
      const page = await readFile(new URL('../web/index.html', import.meta.url));
      response.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
      });
      response.end(page);
    } catch {
      response.writeHead(500).end('Startup page unavailable');
    }
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env['PORT'] ?? 5173);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT must be an integer between 1 and 65535');
  const app = createApp();
  app.on('error', (error) => { console.error(error.message); process.exitCode = 1; });
  app.listen(port, '127.0.0.1', () => console.log(`QMG Mercury: http://127.0.0.1:${port}`));
}
