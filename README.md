# 估值回測儀表板（GOOGL · MSFT · AVGO）

Watchlist 比較多隻股，點選一隻睇完整四課回測。估值序列 = 5Y 滾動中位 P/E × TTM diluted EPS。

## 結構

```
index.html / style.css / js/app.js
vendor/chart.umd.min.js
data/tickers.json           watchlist
data/{ticker}_daily.csv     date,price,valuation
data/last_eps.json          GOOGL TTM EPS（舊檔名）
data/msft_eps.json / avgo_eps.json
build_valuation.py          用 SEC EPS + yfinance 重建歷史估值 CSV
update_data.py              向 yfinance 補缺少的交易日（可指定 symbol）
server.py                   本機頁面 + /api/update
```

## 每日更新流程

頁面載入不會自動抓資料。選好 ticker 後撳「擷取最新數據」。

```bash
pip install -r requirements.txt
python server.py
```

開 http://127.0.0.1:8765/ 。每一次撳掣先向 yfinance 要當前股票收市價同 TTM EPS，並寫入對應 CSV。

- 非業績期：`valuation` 沿用上一行
- `trailingEps` 相對 eps json 變動超過 1%：估值按比率調整
- 中間漏咗幾個交易日：一次補上缺少的日子
- 上線環境若 `data/` 唯讀：改寫暫存目錄；可用 `GOOGL_DATA_DIR` 指定可寫目錄

```bash
python update_data.py          # 預設 GOOGL
python update_data.py MSFT
python update_data.py --all
```

## 重建歷史估值 CSV

```bash
python build_valuation.py MSFT AVGO
python build_valuation.py --all
```

SEC `EarningsPerShareDiluted`（季度）湊 TTM，股價用 yfinance auto_adjust，EPS 會按 split 調到同一基準，再算 5Y×252 滾動中位 P/E × TTM。

## 方法(與估值回測手冊一致)

- gap = (估值 − 股價) ÷ 股價
- 五分位 Q1 最貴 … Q5 最平;前瞻 3M/12M 回報用 t+1 執行
- DCA 三規則:V0 每月照供 / V1 平(Q4–Q5)先供,貴持現金 / V2 平加倍、貴泊現金;訊號用上一個月尾分層(point-in-time)
