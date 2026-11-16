#!/usr/bin/env python3
"""用 SEC diluted EPS + yfinance 股價，重建 5Y 滾動中位 P/E × TTM EPS 估值序列。

輸出:
  data/{symbol}_daily.csv   date,price,valuation
  data/{symbol}_eps.json    最新 TTM EPS 摘要

用法:
  python build_valuation.py MSFT AVGO
  python build_valuation.py --all
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SEC_UA = "googl-valuation-builder/1.0 (research; contact@example.com)"

# 可有多個 CIK（併購前後），會合併季度 EPS
TICKERS = {
    "MSFT": {"ciks": ["0000789019"], "start": "2008-01-01"},
    "AVGO": {"ciks": ["0001730168", "0001441634"], "start": "2009-08-06"},
    "GOOGL": {"ciks": ["0001652044"], "start": "2008-12-30"},
}


def _http_json(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
    try:
        return json.loads(gzip.decompress(raw))
    except OSError:
        return json.loads(raw)


def _quarter_eps_from_sec(cik: str) -> pd.Series:
    """回傳 index=filed date、value=該季 diluted EPS（只取季度時段）。"""
    data = _http_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")
    node = data.get("facts", {}).get("us-gaap", {}).get("EarningsPerShareDiluted")
    if not node:
        return pd.Series(dtype=float)
    units = node.get("units", {})
    rows = units.get("USD/shares") or next(iter(units.values()), [])
    by_end = {}
    for row in rows:
        if row.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
            continue
        start, end, filed = row.get("start"), row.get("end"), row.get("filed")
        if not (start and end and filed):
            continue
        try:
            d0 = datetime.strptime(start[:10], "%Y-%m-%d").date()
            d1 = datetime.strptime(end[:10], "%Y-%m-%d").date()
            filed_d = datetime.strptime(filed[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (d1 - d0).days
        # 只要季度（約 70–120 日），排除全年累計
        if days < 70 or days > 120:
            continue
        val = row.get("val")
        if val is None or not math.isfinite(float(val)):
            continue
        key = d1.isoformat()
        prev = by_end.get(key)
        # 同一季結束日：保留較新 filed（修正稿）
        if prev is None or filed_d >= prev[0]:
            by_end[key] = (filed_d, float(val), d1)

    if not by_end:
        return pd.Series(dtype=float)
    records = sorted(by_end.values(), key=lambda x: x[2])
    # point-in-time：用 filed 日作為可知日
    s = pd.Series(
        {pd.Timestamp(filed_d): eps for filed_d, eps, _end in records},
        dtype=float,
    ).sort_index()
    # 同一 filed 日多季時取最後
    s = s.groupby(level=0).last()
    return s


def _load_quarter_eps(symbol: str) -> pd.Series:
    series = []
    for cik in TICKERS[symbol]["ciks"]:
        try:
            s = _quarter_eps_from_sec(cik)
        except Exception as exc:
            print(f"[warn] SEC {symbol} CIK {cik}: {exc}")
            continue
        if not s.empty:
            series.append(s)
            print(f"[ok] {symbol} CIK {cik}: {len(s)} quarterly EPS points")
    if not series:
        raise RuntimeError(f"{symbol}: 找不到 SEC quarterly diluted EPS")
    out = pd.concat(series).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _prices(symbol: str, start: str) -> pd.Series:
    hist = yf.Ticker(symbol).history(start=start, auto_adjust=True, interval="1d")
    if hist is None or hist.empty or "Close" not in hist.columns:
        raise RuntimeError(f"{symbol}: yfinance 無股價")
    close = hist["Close"].astype(float).copy()
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close[close > 0].sort_index()
    close = close[~close.index.duplicated(keep="last")]
    return close


def _split_adjust_eps(symbol: str, quarter_eps: pd.Series) -> pd.Series:
    """把 as-reported 季度 EPS 調到與 auto_adjust 股價同一股份基準。

    yfinance auto_adjust 會把歷史價除以之後發生的 forward split；
    因此 filed 日之前的 EPS 也要除以同期間累積 split ratio，否則 PE 失真。
    """
    splits = yf.Ticker(symbol).splits
    if splits is None or len(splits) == 0:
        return quarter_eps
    split = splits.astype(float).copy()
    split.index = pd.to_datetime(split.index).tz_localize(None).normalize()
    split = split[split > 0].sort_index()
    if split.empty:
        return quarter_eps

    # 對每個 filed 日：累乘「嚴格晚於 filed」的所有 split
    adj = {}
    for filed, eps in quarter_eps.items():
        filed_ts = pd.Timestamp(filed).normalize()
        future = split[split.index > filed_ts]
        factor = float(future.prod()) if len(future) else 1.0
        adj[filed_ts] = float(eps) / factor
    out = pd.Series(adj, dtype=float).sort_index()
    out = out.groupby(level=0).last()
    return out


def _ttm_eps_daily(index: pd.DatetimeIndex, quarter_eps: pd.Series) -> pd.Series:
    """每個交易日用『已公布』最近 4 個季度 EPS 總和做 TTM。"""
    q = quarter_eps.sort_index()
    # 累積：每個 filed 日更新最新 4 季
    filed_dates = list(q.index)
    values = list(q.values)
    ttm_at_file = {}
    for i, dt in enumerate(filed_dates):
        window = values[max(0, i - 3) : i + 1]
        if len(window) < 4:
            continue
        ttm_at_file[pd.Timestamp(dt).normalize()] = float(sum(window))
    if not ttm_at_file:
        raise RuntimeError("TTM EPS 不足以湊齊 4 季")
    ttm = pd.Series(ttm_at_file).sort_index()
    # 向前填充到交易日；只用已公布值
    daily = ttm.reindex(ttm.index.union(index)).sort_index().ffill()
    daily = daily.reindex(index)
    return daily


def build_symbol(symbol: str) -> pd.DataFrame:
    meta = TICKERS[symbol]
    start = meta["start"]
    print(f"== building {symbol} ==")
    quarter_eps = _load_quarter_eps(symbol)
    quarter_eps = _split_adjust_eps(symbol, quarter_eps)
    price = _prices(symbol, start)
    # 估值序列從有完整 4 季 TTM + 5 年 PE 窗口後先開始輸出；
    # 先對齊價格區間
    ttm = _ttm_eps_daily(price.index, quarter_eps)
    pe = price / ttm.replace(0, np.nan)
    pe = pe.replace([np.inf, -np.inf], np.nan)
    # 負 TTM（虧損）時 PE 無意義：該日落 NaN，中位數忽略
    pe[ttm <= 0] = np.nan
    med = pe.rolling(window=252 * 5, min_periods=252).median()
    valuation = med * ttm
    df = pd.DataFrame(
        {
            "date": price.index.date.astype(str),
            "price": price.round(2).values,
            "valuation": valuation.round(4).values,
            "ttm_eps": ttm.round(4).values,
            "pe": pe.round(4).values,
            "pe_med_5y": med.round(4).values,
        },
        index=price.index,
    )
    # 只要估值可用列（有 5Y 中位 PE）
    out = df.dropna(subset=["valuation", "price"]).copy()
    out = out[out["valuation"] > 0]
    print(
        f"[ok] {symbol}: {len(out)} rows "
        f"{out['date'].iloc[0]} → {out['date'].iloc[-1]} "
        f"last price={out['price'].iloc[-1]} val={out['valuation'].iloc[-1]} "
        f"ttm={out['ttm_eps'].iloc[-1]} pe5y={out['pe_med_5y'].iloc[-1]}"
    )
    return out


def write_outputs(symbol: str, df: pd.DataFrame):
    DATA.mkdir(parents=True, exist_ok=True)
    csv_path = DATA / f"{symbol.lower()}_daily.csv"
    eps_path = DATA / f"{symbol.lower()}_eps.json"
    export = df[["date", "price", "valuation"]].copy()
    export.to_csv(csv_path, index=False, float_format="%.4f")
    # price 保持兩位
    lines = ["date,price,valuation"]
    for row in export.itertuples(index=False):
        lines.append(f"{row.date},{float(row.price):.2f},{float(row.valuation):.4f}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    last = df.iloc[-1]
    sec_ttm = round(float(last["ttm_eps"]), 4)
    payload = {
        "symbol": symbol,
        "ttmEps": sec_ttm,
        "secTtmEps": sec_ttm,
        "asOf": str(last["date"]),
        "peMed5y": round(float(last["pe_med_5y"]), 4),
        "valuation": round(float(last["valuation"]), 4),
        "source": "SEC EarningsPerShareDiluted + yfinance",
        "method": "5Y rolling median PE × TTM diluted EPS",
    }
    # live update 用 yfinance trailingEps；把 baseline 對齊，避免第一次擷取就按比率扭曲估值
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        raw = info.get("trailingEps")
        if raw is not None:
            raw = float(raw)
            if raw == raw and raw > 0:
                payload["ttmEps"] = round(raw, 4)
                payload["yahooTrailingEps"] = round(raw, 4)
        frame = ticker.get_earnings_dates(limit=8)
        today = date.today()
        upcoming = []
        if frame is not None and not frame.empty:
            for ts in frame.index:
                day = ts.date() if hasattr(ts, "date") else None
                if isinstance(day, date) and day >= today:
                    upcoming.append(day)
        if upcoming:
            payload["nextEarnings"] = min(upcoming).isoformat()
    except Exception:
        pass
    eps_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[write] {csv_path} ({csv_path.stat().st_size} bytes)")
    print(f"[write] {eps_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*", help="Tickers, e.g. MSFT AVGO")
    parser.add_argument("--all", action="store_true", help="Build all configured tickers")
    args = parser.parse_args()
    symbols = list(TICKERS) if args.all else [s.upper() for s in args.symbols]
    if not symbols:
        symbols = ["MSFT", "AVGO"]
    for sym in symbols:
        if sym not in TICKERS:
            raise SystemExit(f"未配置 {sym}，可用: {', '.join(TICKERS)}")
        df = build_symbol(sym)
        write_outputs(sym, df)


if __name__ == "__main__":
    main()
