"""Local UI fixture only: serves view assets and a fake bridge, never the repo root."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'views/digest'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.split('?', 1)[0]
        routes = {'/': ('text/html; charset=utf-8', ASSETS / 'index.html'),
                  '/app.js': ('text/javascript; charset=utf-8', ASSETS / 'app.js'),
                  '/style.css': ('text/css; charset=utf-8', ASSETS / 'style.css'),
                  '/fixture.js': ('text/javascript; charset=utf-8', ROOT / 'tests/ui_fixture.js')}
        if name not in routes:
            self.send_error(404)
            return
        kind, path = routes[name]
        content = path.read_text(encoding='utf-8')
        if name == '/':
            content = content.replace('<script type="module"', '<script src="/fixture.js"></script><script type="module"')
        data = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == '__main__':
    print('UI fixture only: http://127.0.0.1:18764', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 18764), Handler).serve_forever()
