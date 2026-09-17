#!/usr/bin/env python3
"""
Mock Server for SYS-UI Dual Columns & Hotspot Testing
Listens on assigned Gemini test port 28820.
Serves static client assets and mock API endpoints according to SYS-UI contracts.
"""
import json
import mimetypes
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

CLIENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "client"))
FIXTURES_PATH = os.path.join(os.path.dirname(__file__), "test_fixtures.json")

def load_fixtures():
    with open(FIXTURES_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)

class MockUIHandler(BaseHTTPRequestHandler):
    fixtures = load_fixtures()
    manual_shares = list(fixtures["manual_shares"])
    system_shares = list(fixtures["system_shares"])
    current_user = fixtures["user_one"]["user"]

    def log_message(self, format, *args):
        # Suppress noisy logs during automated tests
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/me":
            self.send_json({"user": self.current_user})
            return

        if path == "/api/system":
            self.send_json(self.fixtures["system_state"])
            return

        if path == "/api/shares":
            source_kind = qs.get("source_kind", [None])[0]
            if source_kind == "manual":
                self.send_json({"shares": self.manual_shares})
            elif source_kind == "system":
                self.send_json({"shares": self.system_shares})
            else:
                self.send_json({"shares": self.manual_shares + self.system_shares})
            return

        if path.startswith("/api/shares/"):
            share_id = path[len("/api/shares/"):]
            all_shares = self.manual_shares + self.system_shares
            found = next((s for s in all_shares if s["id"] == share_id), None)
            if found:
                self.send_json({"share": found})
            else:
                self.send_json({"error": "分享不存在"}, status=404)
            return

        if path == "/tr/get":
            q = qs.get("q", [""])[0]
            self.send_json({"responseData": {"translatedText": f"【译】{q}"}})
            return

        # Static file serving from client/
        file_path = path.lstrip("/")
        if not file_path or file_path == "index.html":
            target = os.path.join(CLIENT_DIR, "index.html")
        else:
            target = os.path.join(CLIENT_DIR, file_path)

        if os.path.isfile(target):
            mime_type, _ = mimetypes.guess_type(target)
            if not mime_type:
                mime_type = "application/octet-stream"
            with open(target, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path.startswith("/api/shares/") and path.endswith("/read"):
            share_id = path.split("/")[3]
            for s in self.manual_shares + self.system_shares:
                if s["id"] == share_id:
                    s["read"] = True
            self.send_json({"ok": True})
            return

        if path.startswith("/api/shares/") and path.endswith("/reaction"):
            share_id = path.split("/")[3]
            emoji = payload.get("emoji", "")
            for s in self.manual_shares + self.system_shares:
                if s["id"] == share_id:
                    s["reactions"] = [r for r in s["reactions"] if r["user_id"] != self.current_user["id"]]
                    if emoji:
                        s["reactions"].append({"user_id": self.current_user["id"], "emoji": emoji})
            self.send_json({"ok": True})
            return

        if path.startswith("/api/shares/") and path.endswith("/comment"):
            share_id = path.split("/")[3]
            comment_body = payload.get("body", "")
            for s in self.manual_shares + self.system_shares:
                if s["id"] == share_id:
                    s["comments"].append({
                        "name": self.current_user["name"],
                        "body": comment_body,
                        "created": 1773660500
                    })
            self.send_json({"ok": True})
            return

        self.send_json({"error": "未知接口"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/shares/"):
            share_id = path[len("/api/shares/"):]
            self.manual_shares = [s for s in self.manual_shares if s["id"] != share_id]
            self.system_shares = [s for s in self.system_shares if s["id"] != share_id]
            self.send_json({"ok": True, "deleted": share_id})
            return
        self.send_json({"error": "未知接口"}, status=404)

def start_mock_server(port=28820):
    server = HTTPServer(("127.0.0.1", port), MockUIHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 28820
    print(f"Starting SYS-UI Mock Server on http://127.0.0.1:{port} (serving {CLIENT_DIR})")
    server = HTTPServer(("127.0.0.1", port), MockUIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
        server.server_close()
