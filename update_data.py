#!/usr/bin/env python3
"""用 yfinance 按一次請求補上 GOOGL 缺少的交易日。

非業績期 valuation 沿用上一行。trailingEps 相對 data/last_eps.json
變動超過 1% 才按比率調整估值。同一日不會重複新增。

    python update_data.py
"""
import csv
import json
import threading
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "googl_daily.csv"
EPS_PATH = ROOT / "data" / "last_eps.json"
SYMBOL = "GOOGL"
EPS_THRESHOLD = 0.01

_lock = threading.Lock()


def update_once():
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "上一次擷取尚未完成，請稍後再撳"}
    try:
        return _update_once()
    finally:
        _lock.release()


def _update_once():
    try:
        last_date, last_val = _last_row()
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
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

    if bars:
        _append_rows(bars, new_val)
    elif eps_changed:
        _replace_last_valuation(new_val)

    if ttm or next_earn:
        as_of = bars[-1][0] if bars else last_date
        _save_eps(ttm, as_of, next_earn)

    return {
        "ok": True,
        "message": _message(last_date, last_val, new_val, bars, prev, ttm, eps_changed, next_earn),
        "appended": [bar[0] for bar in bars],
        "asof": bars[-1][0] if bars else last_date,
        "valuation": new_val,
        "ttmEps": ttm,
        "prevTtmEps": prev,
        "epsChanged": eps_changed,
        "nextEarnings": next_earn,
    }


def _last_row():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"找不到 {CSV_PATH.name}")
    last = None
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("date") and row.get("price") and row.get("valuation"):
                last = row
    if last is None:
        raise ValueError("CSV 沒有可用的數據列")
    return last["date"].strip(), float(last["valuation"])


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
    if not EPS_PATH.exists():
        return {}
    try:
        data = json.loads(EPS_PATH.read_text(encoding="utf-8"))
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
    EPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": SYMBOL,
        "ttmEps": stored_ttm,
        "asOf": as_of,
        "nextEarnings": stored_earn,
        "source": "yfinance",
    }
    EPS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _newline_style(text):
    return "\r\n" if "\r\n" in text else "\n"


def _append_rows(bars, valuation):
    text = CSV_PATH.read_text(encoding="utf-8")
    newline = _newline_style(text)
    prefix = "" if text.endswith(("\n", "\r\n")) else newline
    body = newline.join(f"{day},{price:.2f},{valuation:.4f}" for day, price in bars)
    with CSV_PATH.open("a", encoding="utf-8", newline="") as handle:
        handle.write(prefix + body + newline)


def _replace_last_valuation(valuation):
    text = CSV_PATH.read_text(encoding="utf-8")
    newline = _newline_style(text)
    lines = text.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    parts = lines[idx].split(",")
    if len(parts) < 3:
        raise ValueError("最後一行 CSV 格式不正確")
    parts[2] = f"{valuation:.4f}"
    lines[idx] = ",".join(parts)
    ending = newline if text.endswith(("\n", "\r\n")) else ""
    CSV_PATH.write_text(newline.join(lines) + ending, encoding="utf-8")


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
