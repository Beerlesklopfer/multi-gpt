import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/conversations/1/messages/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                data = f"data: {i} {time.time():.3f}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                time.sleep(1)
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/download":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", "attachment; filename=x.txt")
            self.send_header("X-Accel-Redirect", "/_protected/media/documents/1/abc.txt")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        if self.path.rstrip("/") == "/mcp":
            # MCP-Server (M15): Antwort als SSE mit Fortschritt
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                data = f"data: {i} {time.time():.3f}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                time.sleep(1)
            self.wfile.write(b"0\r\n\r\n")
            return
        body = f"got {n}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
