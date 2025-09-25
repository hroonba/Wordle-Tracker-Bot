# health.py
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))  # use Render's PORT if set
    HTTPServer(("0.0.0.0", port), H).serve_forever()

