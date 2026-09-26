import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const publicFiles = new Map([
  ['/', ['index.html', 'text/html']], ['/index.html', ['index.html', 'text/html']],
  ...['app.js', 'api.js', 'store.js', 'icons.js'].map(name => [`/src/${name}`, [`src/${name}`, 'text/javascript']]),
  ['/src/styles.css', ['src/styles.css', 'text/css']],
  ...['favicon.svg', 'knowledge-orbit.svg'].map(name => [`/assets/${name}`, [`assets/${name}`, 'image/svg+xml']]),
]);

export function createFrontendServer() {
  return http.createServer(async (req, res) => {
    const port = req.socket.localPort;
    const allowedHosts = [`localhost:${port}`, `127.0.0.1:${port}`];
    const headers = {
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'DENY',
      'Referrer-Policy': 'no-referrer',
      'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
      'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' http://127.0.0.1:8000 http://localhost:8000; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    };
    const respond = (status, body, type = 'text/plain') => {
      const content = Buffer.isBuffer(body) ? body : Buffer.from(body);
      res.writeHead(status, { ...headers, 'Content-Type': `${type}; charset=utf-8`, 'Content-Length': content.length });
      res.end(req.method === 'HEAD' ? undefined : content);
    };
    if (!allowedHosts.includes(req.headers.host)) return respond(403, '请求来源无效');
    if (!['GET', 'HEAD'].includes(req.method)) return respond(405, 'Method not allowed');
    let pathname;
    try { pathname = new URL(req.url, `http://${req.headers.host}`).pathname; }
    catch { return respond(400, 'Invalid request'); }
    const file = publicFiles.get(pathname);
    if (!file) return respond(404, '页面不存在');
    try { respond(200, await readFile(path.join(root, file[0])), file[1]); }
    catch { respond(503, '无法读取页面，请检查 frontend 目录是否完整。'); }
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const index = process.argv.indexOf('--port');
  const port = Number(index >= 0 ? process.argv[index + 1] : process.env.PORT || 5173);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('端口应为 1024—65535 的整数');
  const server = createFrontendServer();
  server.on('error', error => {
    console.error(error.code === 'EADDRINUSE'
      ? `端口 ${port} 已被占用。请停止旧的前端服务，或运行 npm run dev -- --port 5174（更换端口后需配置后端 CORS）。`
      : error.message);
    process.exitCode = 1;
  });
  server.listen(port, '127.0.0.1', () => console.log(`\n  KnowPath 前端已启动\n  http://127.0.0.1:${port}/\n\n  无需安装依赖。按 Ctrl+C 停止。\n`));
}
