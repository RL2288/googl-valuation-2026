#!/usr/bin/env python3
"""本機儀表板。靜態頁面 + POST /api/update（每次請求先向 yfinance 要一次）。

    python server.py
    然後開 http://127.0.0.1:8765/
"""
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from update_data import (
    ROOT,
    SYMBOLS,
    csv_name,
    csv_path,
    eps_name,
    eps_path,
    update_all,
    update_once,
)

HOST = os.environ.get("GOOGL_HOST", "127.0.0.1")
PORT = int(os.environ.get("GOOGL_PORT", "8765"))
MAX_BODY = 4096
SOCKET_TIMEOUT = 60


class Handler(SimpleHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        raw = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw)
        except ValueError:
            return None, "無效 Content-Length"
        if length < 0 or length > MAX_BODY:
            return None, "請求內容太大"
        if length == 0:
            return b"", None
        return self.rfile.read(length), None

    def _data_file(self, path: str):
        """對應 /data/{file} → runtime 可寫路徑（支援 ephemeral）。"""
        if not path.startswith("/data/"):
            return None
        name = path[len("/data/") :]
        if "/" in name or "\\" in name or ".." in name:
            return None
        known = {csv_name(s) for s in SYMBOLS} | {eps_name(s) for s in SYMBOLS}
        if name not in known:
            return None
        # 由檔名反推 symbol
        for sym in SYMBOLS:
            if name == csv_name(sym):
                return csv_path(sym), "text/csv; charset=utf-8"
            if name == eps_name(sym):
                return eps_path(sym), "application/json; charset=utf-8"
        return None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/tickers":
            self._json(
                200,
                {
                    "ok": True,
                    "symbols": list(SYMBOLS),
                    "files": {
                        s: {"csv": f"/data/{csv_name(s)}", "eps": f"/data/{eps_name(s)}"}
                        for s in SYMBOLS
                    },
                },
            )
            return
        served = self._data_file(path)
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
        parsed = urlparse(self.path)
        path = parsed.path
        raw, err = self._read_body()
        if err:
            self._json(413 if "太大" in err else 400, {"ok": False, "message": err})
            return
        if path not in {"/api/update", "/api/update-all"}:
            self.send_error(404)
            return

        symbol = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "message": "JSON 無效"})
                return
            if not isinstance(body, dict):
                self._json(400, {"ok": False, "message": "JSON 要係物件"})
                return
            raw_sym = body.get("symbol")
            if isinstance(raw_sym, str) and raw_sym.strip():
                symbol = raw_sym.strip()

        # Query string 優先：hosted 環境有時 POST body 會丟，query 較穩
        qs = parse_qs(parsed.query or "")
        if qs.get("symbol") and qs["symbol"][0].strip():
            symbol = qs["symbol"][0].strip()

        try:
            if path == "/api/update-all" or (isinstance(symbol, str) and symbol.upper() == "ALL"):
                result = update_all()
            elif not symbol:
                self._json(
                    400,
                    {
                        "ok": False,
                        "message": "請指定 symbol（例如 /api/update?symbol=MSFT）",
                    },
                )
                return
            else:
                result = update_once(symbol)
            status = 200 if result.get("ok") else 502
        except Exception as exc:
            result = {"ok": False, "message": "更新失敗，請稍後再試"}
            status = 500
        self._json(status, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    timeout = SOCKET_TIMEOUT


def main():
    httpd = Server((HOST, PORT), Handler)
    print(f"Valuation dashboard  http://{HOST}:{PORT}/")
    print(f"Watchlist: {', '.join(SYMBOLS)}。撳「擷取最新數據」先會向 yfinance 要一次。Ctrl+C 結束。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
