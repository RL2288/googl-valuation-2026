#!/usr/bin/env python3
"""用 yfinance 按一次請求補上 GOOGL 缺少的交易日。

非業績期 valuation 沿用上一行。trailingEps 相對 data/last_eps.json
變動超過 1% 才按比率調整估值。同一日不會重複新增。

上線環境（例如 /var/task）資料目錄可能唯讀：可寫時寫入 data/，
否則改寫暫存目錄；連暫存都寫不入時仍回傳今次擷取結果畀前端套用。

    python update_data.py
"""
import csv
import errno
import io
import json
import os
import shutil
import threading
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUNDLE_DATA = ROOT / "data"
SYMBOL = "GOOGL"
EPS_THRESHOLD = 0.01
WRITE_ERRNOS = {errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOENT}

_lock = threading.Lock()
_dir_lock = threading.Lock()
_runtime_dir = None
_ephemeral = False


def _can_write(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _seed(directory):
    for name in ("googl_daily.csv", "last_eps.json"):
        dest = directory / name
        src = BUNDLE_DATA / name
        if not dest.exists() and src.exists():
            shutil.copyfile(src, dest)


def _looks_like_readonly_bundle():
    """Cursor / Lambda 等會把程式放喺 /var/task，成個 bundle 唯讀。"""
    try:
        return ROOT.resolve().as_posix().startswith("/var/task")
    except OSError:
        return False


def data_dir():
    """可寫就用原 data/；上線唯讀時改用暫存（可用 GOOGL_DATA_DIR 覆寫）。"""
    global _runtime_dir, _ephemeral
    with _dir_lock:
        if _runtime_dir is not None:
            return _runtime_dir
        override = os.environ.get("GOOGL_DATA_DIR", "").strip()
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            _seed(path)
            _runtime_dir = path
            _ephemeral = path.resolve() != BUNDLE_DATA.resolve()
            return path
        if not _looks_like_readonly_bundle() and _can_write(BUNDLE_DATA):
            _runtime_dir = BUNDLE_DATA
            _ephemeral = False
            return _runtime_dir
        path = Path(os.environ.get("TMPDIR") or os.environ.get("TMP") or "/tmp") / "googl-valuation-data"
        path.mkdir(parents=True, exist_ok=True)
        _seed(path)
        _runtime_dir = path
        _ephemeral = True
        return path


def csv_path():
    return data_dir() / "googl_daily.csv"


def eps_path():
    return data_dir() / "last_eps.json"


def is_ephemeral():
    data_dir()
    return _ephemeral


def update_once():
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "上一次擷取尚未完成，請稍後再撳"}
    try:
        return _update_once()
    finally:
        _lock.release()


def _update_once():
    try:
        rows = _load_rows()
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    if not rows:
        return {"ok": False, "message": "CSV 沒有可用的數據列"}

    last_date = rows[-1]["date"]
    last_val = rows[-1]["valuation"]
    start = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    try:
        bars, ttm, next_earn = _fetch(start)
    except ModuleNotFoundError:
        return {"ok": False, "message": "未安裝 yfinance。請先執行：pip install yfinance"}
    except Exception as exc:
        return {"ok": False, "message": f"yfinance 擷取失敗：{exc}"}
    bars = [(day, price) for day, price in bars if day > last_date]

    prev = _load_eps()
    eps_changed = bool(prev and ttm and abs(ttm / prev - 1) > EPS_THRESHOLD)
    new_val = round(last_val * (ttm / prev), 4) if eps_changed else last_val

    new_rows = [
        {"date": day, "price": price, "valuation": new_val}
        for day, price in bars
    ]
    if bars:
        rows.extend(new_rows)
    elif eps_changed:
        rows[-1] = {**rows[-1], "valuation": new_val}

    as_of = bars[-1][0] if bars else last_date
    persisted = _persist(rows, ttm, as_of, next_earn)
    ephemeral = is_ephemeral()
    message = _message(last_date, last_val, new_val, bars, prev, ttm, eps_changed, next_earn)
    if (bars or eps_changed) and (not persisted or ephemeral):
        message += "部署目錄唯讀，今次結果已回傳畫面；若要留底請撳「下載 CSV」。"

    return {
        "ok": True,
        "message": message,
        "appended": [bar[0] for bar in bars],
        "rows": new_rows,
        "valuationUpdated": bool(eps_changed and not bars),
        "persisted": persisted,
        "ephemeral": ephemeral,
        "asof": as_of,
        "valuation": new_val,
        "ttmEps": ttm,
        "prevTtmEps": prev,
        "epsChanged": eps_changed,
        "nextEarnings": next_earn,
        "lastRow": rows[-1],
    }


def _bundle_or_runtime(name):
    runtime = data_dir() / name
    if runtime.is_file():
        return runtime
    bundled = BUNDLE_DATA / name
    if bundled.is_file():
        return bundled
    return runtime


def _load_rows():
    path = _bundle_or_runtime("googl_daily.csv")
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path.name}")
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (row.get("date") and row.get("price") and row.get("valuation")):
                continue
            try:
                rows.append(
                    {
                        "date": row["date"].strip(),
                        "price": float(row["price"]),
                        "valuation": float(row["valuation"]),
                    }
                )
            except ValueError:
                continue
    rows.sort(key=lambda item: item["date"])
    return rows


def _rows_to_csv(rows):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["date", "price", "valuation"])
    for row in rows:
        writer.writerow(
            [
                row["date"],
                f"{float(row['price']):.2f}",
                f"{float(row['valuation']):.4f}",
            ]
        )
    return buf.getvalue()


def _persist(rows, ttm, as_of, next_earn):
    try:
        path = csv_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_rows_to_csv(rows), encoding="utf-8")
        if ttm or next_earn:
            _save_eps(ttm, as_of, next_earn)
        return True
    except OSError as exc:
        if exc.errno in WRITE_ERRNOS or getattr(exc, "errno", None) is None:
            return False
        # 其他 I/O 錯誤都唔阻礙前端套用今次結果
        return False


def _fetch(start):
    import yfinance as yf

    ticker = yf.Ticker(SYMBOL)
    hist = ticker.history(start=start, auto_adjust=False, interval="1d")
    bars = []
    if hist is not None and not hist.empty and "Close" in hist.columns:
        for ts, row in hist.iterrows():
            close = float(row["Close"])
            if close != close or close <= 0:  # NaN
                continue
            bars.append((ts.date().isoformat(), round(close, 2)))
        bars.sort(key=lambda item: item[0])

    ttm = None
    try:
        info = ticker.info or {}
        raw = info.get("trailingEps")
        if raw is not None:
            raw = float(raw)
            if raw == raw and raw > 0:
                ttm = raw
    except Exception:
        ttm = None
    return bars, ttm, _next_earnings(ticker)


def _next_earnings(ticker):
    """下一個尚未公布的業績日。日曆上已過期的日期唔用。"""
    today = date.today()
    upcoming = []
    try:
        frame = ticker.get_earnings_dates(limit=12)
        if frame is not None and not frame.empty:
            for ts in frame.index:
                day = ts.date() if hasattr(ts, "date") else None
                if isinstance(day, date) and day >= today:
                    upcoming.append(day)
    except Exception:
        pass
    if not upcoming:
        try:
            cal = ticker.calendar or {}
            raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if not isinstance(raw, (list, tuple)):
                raw = [raw]
            for item in raw:
                if isinstance(item, date) and item >= today:
                    upcoming.append(item)
        except Exception:
            pass
    return min(upcoming).isoformat() if upcoming else None


def _read_eps_file():
    path = _bundle_or_runtime("last_eps.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_eps():
    try:
        value = float(_read_eps_file().get("ttmEps"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _save_eps(ttm, as_of, next_earnings):
    prev = _read_eps_file()
    stored_ttm = round(ttm, 4) if ttm else prev.get("ttmEps")
    stored_earn = next_earnings or prev.get("nextEarnings")
    if stored_ttm is None and not stored_earn:
        return
    path = eps_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": SYMBOL,
        "ttmEps": stored_ttm,
        "asOf": as_of,
        "nextEarnings": stored_earn,
        "source": "yfinance",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _message(last_date, last_val, new_val, bars, prev, ttm, eps_changed, next_earn):
    if bars:
        span = bars[0][0] if len(bars) == 1 else f"{bars[0][0]} → {bars[-1][0]}"
        head = f"已補上 {span}，共 {len(bars)} 個交易日"
    else:
        head = f"已是最新（{last_date}），沒有新交易日"

    if ttm is None:
        body = f"{head}。TTM EPS 暫時攞唔到，估值沿用 {last_val:.4f}。"
    elif prev is None:
        body = f"{head}。已記下 TTM EPS {ttm:.2f}；未有上次數值，估值未改。"
    elif eps_changed:
        where = "新交易日" if bars else "最後一行"
        body = (
            f"{head}。TTM EPS {prev:.2f} → {ttm:.2f}，"
            f"估值調為 {new_val:.4f}（套用到{where}）。"
        )
    else:
        body = f"{head}。TTM EPS {ttm:.2f} 變動未超過 1%，估值沿用 {last_val:.4f}。"
    if next_earn:
        return f"{body}下次業績公布 {next_earn}。"
    return f"{body}下次業績公布日期暫時未有。"


def main():
    result = update_once()
    print(result["message"] if result.get("message") else result)
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
