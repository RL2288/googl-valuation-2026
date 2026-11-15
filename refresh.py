#!/usr/bin/env python3
"""
refresh.py — 每日重算 GOOGL 估值回測數據，輸出 data/data.json 畀 dashboard 用。

用法:
    python3 refresh.py [csv路徑]          # 預設 data/googl_daily.csv

CSV 欄位: date, price, valuation
輸出:
    data/data.json  — 月度價格/估值、每日 gap、p25/p75、五分位前瞻回報、
                      DCA 策略淨值 (V0 每月照供 / V1 平先供 / V2 平加碼貴泊停)

方法與估值回測手冊一致:
    - gap = (valuation - price) / price
    - 五分位 Q1 最貴 ... Q5 最平；前瞻 3M/12M 回報用 t+1 執行 (翌日起計)
    - 策略月供,訊號用上一個月尾 gap (point-in-time,無未來函數)
"""
import json
import sys
import numpy as np
import pandas as pd

CSV = sys.argv[1] if len(sys.argv) > 1 else "data/googl_daily.csv"
OUT = "data/data.json"

BUDGET = 1000.0          # V0 / V1 每月月供
V2_CHEAP_MULT = 2.0      # V2 平時供兩倍
V2_CASH = True           # V2 貴時停泊轉現金
CHEAP_TIER = (4, 5)      # gap 分層 4/5 = 平


def load(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    need = {"date", "price", "valuation"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"CSV 缺少欄位: {missing}")
    df["gap"] = (df.valuation - df.price) / df.price
    df = df.dropna(subset=["gap"]).reset_index(drop=True)
    return df


def fwd_returns(df: pd.DataFrame, months: int) -> pd.Series:
    """t+1 執行:回報 = price[t + N 營業日] / price[t+1] - 1,N≈21/月"""
    n = 21 * months
    price = df.price.to_numpy()
    out = np.full(len(df), np.nan)
    idx = np.arange(1, len(df) - n)
    out[idx] = price[idx + n] / price[idx] - 1.0
    return pd.Series(out, index=df.index)


def quintile_stats(df: pd.DataFrame):
    try:
        q = pd.qcut(df.gap, 5, labels=False, duplicates="drop") + 1  # 1=最貴
    except ValueError:
        q = pd.qcut(df.gap.rank(method="first"), 5, labels=False) + 1
    r3 = fwd_returns(df, 3)
    r12 = fwd_returns(df, 12)
    rows = []
    for tier in range(1, 6):
        m = q == tier
        sub12 = r12[m].dropna()
        rows.append(dict(
            tier=tier,
            gapMin=round(float(df.gap[m].min()) * 100, 1),
            gapMax=round(float(df.gap[m].max()) * 100, 1),
            avg3m=round(float(r3[m].mean()) * 100, 2),
            avg12m=round(float(r12[m].mean()) * 100, 2),
            win12m=round(float((sub12 > 0).mean()) * 100, 1),
            n=int(m.sum()),
        ))
    edges = [round(float(v) * 100, 1) for v in pd.qcut(df.gap, 5).cat.categories.left.tolist()]
    edges.append(round(float(df.gap.max()) * 100, 1))
    return rows, q.to_numpy(), edges


def simulate(monthly: pd.DataFrame, tier_monthly: np.ndarray, budget: float = BUDGET):
    """三條 DCA 規則,訊號用上一個月尾分層(第一個月無訊號按 V0 照供)。"""
    n = len(monthly)
    prices = monthly.price.to_numpy()
    units = {k: 0.0 for k in ("V0", "V1", "V2")}
    cash = {"V0": 0.0, "V1": 0.0, "V2": 0.0}
    invested = 0.0
    curves = {"V0": [], "V1": [], "V2": [], "invested": []}
    for i in range(n):
        invested += budget
        t = tier_monthly[i - 1] if i > 0 else 3  # prior month-end signal
        # V0 照供
        units["V0"] += budget / prices[i]
        # V1 平先供
        if t in CHEAP_TIER:
            amt = cash["V1"] + budget
            cash["V1"] = 0.0
        else:
            amt = 0.0
            cash["V1"] += budget
        units["V1"] += amt / prices[i]
        # V2 平加倍、貴停泊
        if t in CHEAP_TIER:
            amt = cash["V2"] + budget * (V2_CHEAP_MULT if V2_CASH else 1.0)
            cash["V2"] = 0.0
        else:
            amt = 0.0
            cash["V2"] += budget
        units["V2"] += amt / prices[i]
        for k in units:
            curves[k].append(round(units[k] * prices[i], 2))
        curves["invested"].append(round(invested, 2))
    return curves


def irr_monthly(final: float, n_months: int, budget: float) -> float:
    """月 IRR -> 年化。二分法解 sum(budget*(1+r)^(n-1-i)) = final"""
    lo, hi = -0.5, 0.5
    for _ in range(200):
        mid = (lo + hi) / 2
        fv = sum(budget * (1 + mid) ** (n_months - 1 - i) for i in range(n_months))
        if fv < final:
            lo = mid
        else:
            hi = mid
    return (1 + mid) ** 12 - 1


def main():
    df = load(CSV)
    g = df.gap

    monthly = df.set_index("date").resample("ME").last()[["price", "valuation"]].dropna()
    monthly["gap"] = (monthly.valuation - monthly.price) / monthly.price

    qstats, q_arr, edges = quintile_stats(df)
    q_monthly = df.set_index("date").resample("ME").last()
    q_monthly["tier"] = pd.cut(q_monthly.gap, bins=[-np.inf] + [e / 100 for e in edges[1:-1]] + [np.inf],
                               labels=[1, 2, 3, 4, 5]).astype(int)
    tier_m = q_monthly.tier.to_numpy()

    curves = simulate(monthly.reset_index(), tier_m)
    n = len(monthly)
    last_g = float(g.iloc[-1])
    last_tier = int(pd.cut([last_g], bins=[-np.inf] + [e / 100 for e in edges[1:-1]] + [np.inf],
                           labels=[1, 2, 3, 4, 5])[0])
    p25, p75 = float(g.quantile(0.25)) * 100, float(g.quantile(0.75)) * 100

    stats = {}
    for k in ("V0", "V1", "V2"):
        final = curves[k][-1]
        inv = curves["invested"][-1]
        stats[k] = dict(
            final=round(final), multiple=round(final / inv, 2),
            irr=round(irr_monthly(final, n, BUDGET) * 100, 1),
        )

    out = dict(
        asof=str(df.date.iloc[-1].date()),
        start=str(df.date.iloc[0].date()),
        nDays=int(len(df)),
        kpi=dict(
            price=round(float(df.price.iloc[-1]), 2),
            valuation=round(float(df.valuation.iloc[-1]), 2),
            gapPct=round(last_g * 100, 2),
            percentile=round(float((g < last_g).mean()) * 100, 1),
            tier=last_tier,
        ),
        bands=dict(p25=round(p25, 1), p75=round(p75, 1),
                   median=round(float(g.median()) * 100, 1)),
        quintileEdges=edges,
        quintiles=qstats,
        monthly=dict(
            dates=[str(d.date()) for d in monthly.index],
            price=[round(float(v), 2) for v in monthly.price],
            valuation=[round(float(v), 2) for v in monthly.valuation],
            gap=[round(float(v) * 100, 2) for v in monthly.gap],
            tiers=[int(t) for t in tier_m],
        ),
        curves=curves,
        simStats=stats,
        invested=round(curves["invested"][-1]),
        months=n,
    )
    json.dump(out, open(OUT, "w"), ensure_ascii=False)
    print(f"[OK] {OUT} 已更新 — asof={out['asof']} gap={out['kpi']['gapPct']}% tier=Q{last_tier} n={out['nDays']}日/{n}月")


if __name__ == "__main__":
    main()
