#!/usr/bin/env python3
"""本機儀表板。靜態頁面 + POST /api/update（每次請求先向 yfinance 要一次）。

    python server.py
    然後開 http://127.0.0.1:8765/
"""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from update_data import ROOT, csv_path, eps_path, update_once

HOST = "127.0.0.1"
PORT = 8765


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        served = {
            "/data/googl_daily.csv": (csv_path(), "text/csv; charset=utf-8"),
            "/data/last_eps.json": (eps_path(), "application/json; charset=utf-8"),
        }.get(path)
        if served is None:
            return super().do_GET()
        file_path, content_type = served
        if not file_path.is_file():
            return super().do_GET()
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        if path != "/api/update":
            self.send_error(404)
            return
        try:
            result = update_once()
            status = 200 if result.get("ok") else 502
        except Exception as exc:
            result = {"ok": False, "message": f"更新失敗：{exc}"}
            status = 500
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"GOOGL dashboard  http://{HOST}:{PORT}/")
    print("撳「擷取最新數據」先會向 yfinance 要一次。Ctrl+C 結束。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
