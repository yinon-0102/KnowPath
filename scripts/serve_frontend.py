"""Serve only KnowPath's index.html on the loopback interface.

This is deliberately not a general-purpose static file server: configuration,
tokens, source files, and directory listings must never be reachable by HTTP.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 5173
API_URL = "http://127.0.0.1:8000"
SERVICE_ID = "knowpath-index-only-v1"


def make_handler(index_file: Path, port: int = PORT):
    """Bind one specific file, never a directory or a user-controlled path."""

    class IndexHandler(BaseHTTPRequestHandler):
        server_version = "KnowPathLocal/1"
        sys_version = ""

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("X-KnowPath-Service", SERVICE_ID)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
                "style-src 'unsafe-inline'; connect-src http://127.0.0.1:8000 http://localhost:8000; "
                "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            super().end_headers()

        def log_message(self, format, *args):
            # Do not log URLs, query strings, headers, or accidental credentials.
            return

        def _respond(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve(self):
            if self.headers.get("Host") not in {
                f"127.0.0.1:{port}", f"localhost:{port}"
            }:
                self._respond(403, "请求来源无效".encode(), "text/plain; charset=utf-8")
                return
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                self._respond(400, b"Invalid request", "text/plain; charset=utf-8")
                return
            if parsed.scheme or parsed.netloc or parsed.path not in {"/", "/index.html"}:
                self._respond(404, "页面不存在".encode(), "text/plain; charset=utf-8")
                return
            try:
                body = index_file.read_bytes()
            except OSError:
                self._respond(503, "无法读取首页".encode(), "text/plain; charset=utf-8")
                return
            self._respond(200, body, "text/html; charset=utf-8")

        def do_GET(self):
            self._serve()

        def do_HEAD(self):
            self._serve()

    return IndexHandler


def open_frontend():
    """Read the same dotenv/token configuration as the API; never log the token."""
    from dotenv import load_dotenv

    backend = ROOT / "py"
    load_dotenv(backend / ".env", override=False)
    token = os.getenv("LEARNING_LOCAL_TOKEN")
    if not token:
        configured = os.getenv("LEARNING_LOCAL_TOKEN_FILE")
        token_file = Path(configured) if configured else backend / ".learning-token.local"
        if not token_file.is_absolute():
            token_file = backend / token_file
        token = token_file.read_text(encoding="utf-8").strip()
    if not token or any(char.isspace() for char in token):
        raise RuntimeError("本地会话令牌无效，请检查后端配置。")

    # The PowerShell launcher has already checked the owning process and health.
    # Verify authenticated API identity before handing the token to the browser.
    request = Request(API_URL + "/openapi.json", headers={"X-Local-Token": token})
    with urlopen(request, timeout=15) as response:
        schema = json.load(response)
    if schema.get("info", {}).get("title") != "Keel Learning" or "/api/v1/health" not in schema.get("paths", {}):
        raise RuntimeError("8000 端口未返回预期的 KnowPath API。")

    # URL fragments are not sent in HTTP requests. The page imports the token
    # into sessionStorage and immediately removes the fragment from history.
    if not webbrowser.open(f"http://{HOST}:{PORT}/#local_token={quote(token, safe='')}"):
        raise RuntimeError("无法自动打开浏览器，请从页面输入本地会话令牌。")


def main():
    parser = argparse.ArgumentParser(description="KnowPath 本地首页服务")
    parser.add_argument("--open-browser", action="store_true", help="仅用本地会话令牌打开已启动的前端")
    args = parser.parse_args()
    if args.open_browser:
        try:
            open_frontend()
        except Exception:
            # Do not emit tracebacks, token-bearing URLs, or configuration data.
            raise SystemExit("无法打开已认证页面，请确认 API 已启动且本地令牌配置一致。")
        return
    if not (ROOT / "index.html").is_file():
        raise SystemExit("项目根目录缺少 index.html。")
    with ThreadingHTTPServer((HOST, PORT), make_handler(ROOT / "index.html")) as server:
        print(f"KnowPath 前端已启动：http://{HOST}:{PORT}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
