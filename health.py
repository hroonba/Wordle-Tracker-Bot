# health.py
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "10000"))  # Render injects PORT

class Handler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        if self.path == "/healthz" or self.path == "/":
            self._ok()
        else:
            self.send_response(404); self.end_headers()

    def do_HEAD(self):
        if self.path == "/healthz" or self.path == "/":
            self.send_response(200); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    # Optional: silence default request logging
    def log_message(self, *args, **kwargs):
        return

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
