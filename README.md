# GOOGL 估值回測儀表板

每日更新 GOOGL 數據後重算估值回測,圖像化四課分析。

## 結構

```
index.html          頁面 + 結構
style.css           樣式(深色金融主題)
js/app.js           圖表 + 統計 + 上傳重算邏輯
vendor/chart.umd.min.js   Chart.js 4(本地化)
data/googl_daily.csv      每日原始數據(date, price, valuation)
data/last_eps.json        上次 TTM EPS（yfinance trailingEps）
update_data.py            向 yfinance 補缺少的交易日
server.py                 本機頁面 +「擷取最新數據」
refresh.py                可選,另存 data/data.json（頁面不讀這個檔）
```

## 每日更新流程

頁面載入不會自動抓資料。每次要更新先撳一下。

```bash
pip install -r requirements.txt
python server.py
```

開 http://127.0.0.1:8765/ ，撳「擷取最新數據」。每一次撳掣先向 yfinance 要一次 GOOGL 收市價同 TTM EPS，並寫入 CSV。

- 非業績期：`valuation` 沿用上一行
- `trailingEps` 相對 `data/last_eps.json` 變動超過 1%：估值按比率調整
- 中間漏咗幾個交易日：一次補上缺少的日子，唔會重複已有日期
- 「手動輸入」只補一日收市價，唔會呼叫 yfinance
- 「下載 CSV」把目前數據存到電腦
- 上線環境若 `data/` 唯讀（例如 `/var/task`）：改寫暫存目錄，並把今次新列回傳畀頁面套用；要留底就撳「下載 CSV」。可用環境變數 `GOOGL_DATA_DIR` 指定可寫目錄。

唔開頁面也可以只更新一次：

```bash
python update_data.py
```

## 方法(與估值回測手冊一致)

- gap = (估值 − 股價) ÷ 股價
- 五分位 Q1 最貴 … Q5 最平;前瞻 3M/12M 回報用 t+1 執行
- DCA 三規則:V0 每月照供 / V1 平(Q4–Q5)先供,貴持現金 / V2 平加倍、貴泊現金;訊號用上一個月尾分層(point-in-time)
