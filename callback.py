from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

class Callback(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        challenge = query.get("hub.challenge", [""])[0]

        self.send_response(200)
        self.end_headers()
        self.wfile.write(challenge.encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        print("\nWebSub notification received:")
        print(body.decode())

        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass

HTTPServer(("127.0.0.1", 5001), Callback).serve_forever()