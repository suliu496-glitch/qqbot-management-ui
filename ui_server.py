"""Loopback-only management UI, hosted inside the bot process."""
import hmac
import json
import mimetypes
import os
import secrets
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC = Path(__file__).parent / 'webui'


def create_server(service, port=8765):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def finish(self):
            try:
                super().finish()
            finally:
                # SQLite connections are per-thread; close them in that thread.
                if hasattr(service, 'db'):
                    service.db.close_db()

        def log_message(self, *args):
            pass  # Never log bootstrap credentials or contact data.

        def reply(self, code, body, content_type='application/json; charset=utf-8', cookie=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            host = f'127.0.0.1:{self.server.server_port}'
            return self.headers.get('Host') == host and self.headers.get('Origin', f'http://{host}') == f'http://{host}'

        def authenticated(self):
            try:
                cookies = SimpleCookie(self.headers.get('Cookie', ''))
                value = cookies['qqbot_session'].value if 'qqbot_session' in cookies else ''
                return hmac.compare_digest(value, token)
            except Exception:
                return False

        def do_GET(self):
            if not self.trusted():
                return self.reply(403, {'error': '仅允许本机管理入口访问'})
            path = urlsplit(self.path).path
            if path.startswith('/api/'):
                if not self.authenticated():
                    return self.reply(401, {'error': '请通过启动脚本打开管理界面'})
                if path == '/api/session':
                    return self.reply(200, {'csrf': csrf, 'mode': '本机管理员'})
                try:
                    result = service.read(path[5:], parse_qs(urlsplit(self.path).query))
                    return self.reply(200, result)
                except ValueError as exc:
                    return self.reply(400, {'error': str(exc)})
                except Exception:
                    return self.reply(500, {'error': '读取失败，请检查机器人日志与数据文件'})
            allowed = {'/': 'index.html', '/app.js': 'app.js', '/pages.js': 'pages.js', '/style.css': 'style.css'}
            if path not in allowed:
                return self.reply(404, {'error': '页面不存在'})
            file = STATIC / allowed[path]
            return self.reply(200, file.read_bytes(), (mimetypes.guess_type(file)[0] or 'text/plain') + '; charset=utf-8')

        def do_POST(self):
            if not self.trusted():
                return self.reply(403, {'error': '请求来源不匹配'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 100_000:
                    return self.reply(413, {'error': '请求大小超出限制'})
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.reply(415, {'error': '需要 JSON 请求'})
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('请求格式错误')
            except (ValueError, UnicodeError):
                return self.reply(400, {'error': '请求格式错误'})
            if self.path == '/api/login':
                if not hmac.compare_digest(str(payload.get('token', '')), token):
                    return self.reply(401, {'error': '入口凭据已失效，请重新打开管理界面'})
                return self.reply(200, {'csrf': csrf}, cookie=f'qqbot_session={token}; HttpOnly; SameSite=Strict; Path=/')
            if not self.authenticated() or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf):
                return self.reply(403, {'error': '管理会话已失效，请重新打开界面'})
            try:
                result = service.write(self.path.removeprefix('/api/'), payload)
                return self.reply(200, result)
            except ValueError as exc:
                return self.reply(400, {'error': str(exc)})
            except Exception:
                return self.reply(500, {'error': '操作失败，请刷新确认当前数据后再处理'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.bootstrap_url = f'http://127.0.0.1:{server.server_port}/#token={token}'
    return server


def start_ui():
    from ui_service import ManagementService
    server = create_server(ManagementService(), int(os.getenv('QQBOT_UI_PORT', '8765')))
    threading.Thread(target=server.serve_forever, name='qqbot-ui', daemon=True).start()
    if os.getenv('QQBOT_UI_OPEN', '1') == '1':
        webbrowser.open(server.bootstrap_url)
    return server
