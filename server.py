#!/usr/bin/env python3
"""本機儀表板。靜態頁面 + PIN 保護嘅 POST /api/update。

需要環境變數 GOOGL_ACCESS_PIN；未設定時拒絕解鎖／讀數據／更新（fail closed）。

    export GOOGL_ACCESS_PIN=...
    python server.py
    然後開 http://127.0.0.1:8765/
"""
import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from update_data import ROOT, csv_path, eps_path, update_once

HOST = os.environ.get("GOOGL_HOST", "127.0.0.1")
PORT = int(os.environ.get("GOOGL_PORT", "8765"))
ACCESS_PIN = os.environ.get("GOOGL_ACCESS_PIN", "").strip()
COOKIE_NAME = "googl_access"
MAX_BODY = 4096
SOCKET_TIMEOUT = 60
UNLOCK_WINDOW_SEC = 60
UNLOCK_MAX_ATTEMPTS = 5
UNLOCK_LOCKOUT_SEC = 300
PROTECTED_NAMES = frozenset({"googl_daily.csv", "last_eps.json"})

_unlock_lock = threading.Lock()
_unlock_hits = defaultdict(deque)  # ip -> timestamps of failed attempts
_unlock_lockout_until = {}  # ip -> unix time


def _pin_configured():
    return bool(ACCESS_PIN)


def _access_token():
    if not _pin_configured():
        return ""
    return hmac.new(
        ACCESS_PIN.encode("utf-8"),
        b"googl-valuation-access",
        hashlib.sha256,
    ).hexdigest()[:40]


def _pin_ok(candidate):
    if not _pin_configured() or not isinstance(candidate, str) or not candidate:
        return False
    left = hashlib.sha256(candidate.encode("utf-8")).digest()
    right = hashlib.sha256(ACCESS_PIN.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def _normalize_url_path(raw_path):
    path = unquote((raw_path or "").split("?", 1)[0])
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def _resolve_under_root(url_path):
    rel = _normalize_url_path(url_path).lstrip("/")
    if not rel:
        return ROOT.resolve()
    candidate = (ROOT / rel).resolve()
    root = ROOT.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _client_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or handler.client_address[0]
    return handler.client_address[0]


def _unlock_blocked(ip):
    now = time.time()
    with _unlock_lock:
        until = _unlock_lockout_until.get(ip, 0)
        if until > now:
            return True, int(until - now)
        hits = _unlock_hits[ip]
        while hits and now - hits[0] > UNLOCK_WINDOW_SEC:
            hits.popleft()
        return False, 0


def _record_unlock_failure(ip):
    now = time.time()
    with _unlock_lock:
        hits = _unlock_hits[ip]
        hits.append(now)
        while hits and now - hits[0] > UNLOCK_WINDOW_SEC:
            hits.popleft()
        if len(hits) >= UNLOCK_MAX_ATTEMPTS:
            _unlock_lockout_until[ip] = now + UNLOCK_LOCKOUT_SEC
            hits.clear()
            return True
    return False


def _clear_unlock_failures(ip):
    with _unlock_lock:
        _unlock_hits.pop(ip, None)
        _unlock_lockout_until.pop(ip, None)


class Handler(SimpleHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _json(self, status, payload, set_cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
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

    def _authorized(self):
        if not _pin_configured():
            return False
        pin = self.headers.get("X-Access-Pin", "")
        if _pin_ok(pin):
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", "") or "")
        except Exception:
            return False
        morsel = cookie.get(COOKIE_NAME)
        if not morsel:
            return False
        token = _access_token()
        if not token:
            return False
        return hmac.compare_digest(morsel.value, token)

    def _serve_protected(self, file_path, content_type):
        if not _pin_configured():
            self._json(503, {"ok": False, "message": "伺服器未設定 GOOGL_ACCESS_PIN"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "message": "需要 PIN 先可讀取數據"})
            return
        if not file_path.is_file():
            self.send_error(404)
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = _normalize_url_path(self.path)
        runtime_routes = {
            "/data/googl_daily.csv": (csv_path(), "text/csv; charset=utf-8"),
            "/data/last_eps.json": (eps_path(), "application/json; charset=utf-8"),
        }
        if path in runtime_routes:
            self._serve_protected(*runtime_routes[path])
            return

        resolved = _resolve_under_root(path)
        if resolved is not None and resolved.is_file() and resolved.name in PROTECTED_NAMES:
            ctype = (
                "text/csv; charset=utf-8"
                if resolved.name.endswith(".csv")
                else "application/json; charset=utf-8"
            )
            self._serve_protected(resolved, ctype)
            return

        # 任何落到 data/ 目錄嘅請求都要 PIN（避免編碼／相對路徑繞過）
        if resolved is not None:
            data_root = (ROOT / "data").resolve()
            try:
                resolved.relative_to(data_root)
            except ValueError:
                pass
            else:
                if not _pin_configured():
                    self._json(503, {"ok": False, "message": "伺服器未設定 GOOGL_ACCESS_PIN"})
                    return
                if not self._authorized():
                    self._json(401, {"ok": False, "message": "需要 PIN 先可讀取數據"})
                    return

        # 用正規化路徑交畀靜態 handler，避免 %2e%2e 等變形
        self.path = path + (("?" + self.path.split("?", 1)[1]) if "?" in self.path else "")
        return super().do_GET()

    def do_POST(self):
        path = _normalize_url_path(self.path)
        raw, err = self._read_body()
        if err:
            self._json(413 if "太大" in err else 400, {"ok": False, "message": err})
            return

        if path == "/api/unlock":
            if not _pin_configured():
                self._json(503, {"ok": False, "message": "伺服器未設定 GOOGL_ACCESS_PIN"})
                return
            ip = _client_ip(self)
            blocked, wait = _unlock_blocked(ip)
            if blocked:
                self._json(
                    429,
                    {"ok": False, "message": f"嘗試次數過多，請 {wait} 秒後再試"},
                )
                return
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "message": "PIN 格式不正確"})
                return
            if not _pin_ok(data.get("pin")):
                locked = _record_unlock_failure(ip)
                msg = "嘗試次數過多，請稍後再試" if locked else "PIN 不正確"
                self._json(429 if locked else 401, {"ok": False, "message": msg})
                return
            _clear_unlock_failures(ip)
            cookie = (
                f"{COOKIE_NAME}={_access_token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"
            )
            self._json(200, {"ok": True, "message": "已解鎖"}, set_cookie=cookie)
            return

        if path != "/api/update":
            self.send_error(404)
            return
        if not _pin_configured():
            self._json(503, {"ok": False, "message": "伺服器未設定 GOOGL_ACCESS_PIN"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "message": "需要 PIN 先可更新數據"})
            return
        try:
            result = update_once()
            status = 200 if result.get("ok") else 502
        except Exception as exc:
            result = {"ok": False, "message": f"更新失敗：{exc}"}
            status = 500
        self._json(status, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    timeout = SOCKET_TIMEOUT


def main():
    if not _pin_configured():
        print("警告：未設定 GOOGL_ACCESS_PIN，解鎖／數據／更新會全部拒絕。")
    httpd = Server((HOST, PORT), Handler)
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
