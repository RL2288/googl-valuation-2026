/* GOOGL 估值回測儀表板 — app.js
 * 讀 data/googl_daily.csv → 四張圖 + KPI + 兩張表 + DCA 模擬
 * 「擷取最新數據」每次撳一下先 POST /api/update（server.py + yfinance）
 */
"use strict";

const NT = "'Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif";
const MONO = "'SF Mono','Cascadia Code',Consolas,monospace";
const C = {
  text: "#e6e9ee", dim: "#8b95a3", faint: "#5d6772", grid: "rgba(139,149,163,.12)",
  price: "#4c8dff", val: "#d97a4a", green: "#3fb27f", red: "#e5534b",
  gold: "#c9a227", blue: "#4c8dff",
};

let D = null;                 // 計算結果
let DAILY = [];               // 每日原始 rows {date,price,valuation}
let CH = {};                  // Chart instances
let dirty = false;            // 手動新增但未下載 CSV
let accessPin = sessionStorage.getItem("googl_pin") || "";
const fmt$ = (v) => "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (v, d = 1) => (v > 0 ? "+" : "") + Number(v).toFixed(d) + "%";

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (accessPin) headers["X-Access-Pin"] = accessPin;
  return headers;
}
function authFetch(url, options = {}) {
  const next = { credentials: "include", ...options };
  next.headers = authHeaders(options.headers || {});
  return fetch(url, next);
}

Chart.defaults.color = C.dim;
Chart.defaults.font.family = NT;
Chart.defaults.font.size = 11.5;
Chart.defaults.borderColor = C.grid;
Chart.defaults.animation.duration = 350;

/* ---------- CSV 解析(瀏覽器端) ---------- */
function parseCSV(text) {
  const lines = text.trim().split(/\r?\n/);
  const head = lines[0].split(",").map((s) => s.trim().toLowerCase());
  const iD = head.indexOf("date"), iP = head.indexOf("price"), iV = head.indexOf("valuation");
  if (iD < 0 || iP < 0 || iV < 0) throw new Error("CSV 需要 date, price, valuation 三欄");
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const c = lines[i].split(",");
    const p = parseFloat(c[iP]), v = parseFloat(c[iV]);
    if (c[iD] && isFinite(p) && isFinite(v)) rows.push({ date: c[iD].trim(), price: p, valuation: v });
  }
  rows.sort((a, b) => (a.date < b.date ? -1 : 1));
  return rows;
}

/* ---------- 統計(對應 refresh.py) ---------- */
function quantile(sorted, q) {
  const i = (sorted.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}
function computeAll(rows, budget = 1000) {
  const n = rows.length;
  const gaps = rows.map((r) => (r.valuation - r.price) / r.price);
  const sorted = [...gaps].sort((a, b) => a - b);
  const edges = [0.2, 0.4, 0.6, 0.8].map((q) => quantile(sorted, q));
  const tierOf = (g) => (g <= edges[0] ? 1 : g <= edges[1] ? 2 : g <= edges[2] ? 3 : g <= edges[3] ? 4 : 5);

  // 月度(月尾)
  const m = {};
  rows.forEach((r, i) => { m[r.date.slice(0, 7)] = { date: r.date, price: r.price, valuation: r.valuation, gap: gaps[i] }; });
  const mon = Object.values(m);
  const mGaps = mon.map((x) => x.gap);

  // 前瞻回報 t+1, 3M≈63, 12M≈252 交易日
  const fwd = (nDays) => rows.map((_, i) => {
    const j = i + 1, k = j + nDays;
    return k < n && j < n ? rows[k].price / rows[j].price - 1 : null;
  });
  const r3 = fwd(63), r12 = fwd(252);
  const tiers = gaps.map(tierOf);
  const quintiles = [1, 2, 3, 4, 5].map((t) => {
    const idx = tiers.map((x, i) => (x === t ? i : -1)).filter((i) => i >= 0);
    const pick = (arr) => idx.map((i) => arr[i]).filter((v) => v != null);
    const avg = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : NaN);
    const a12 = pick(r12);
    return {
      tier: t,
      gapMin: Math.min(...idx.map((i) => gaps[i])) * 100,
      gapMax: Math.max(...idx.map((i) => gaps[i])) * 100,
      avg3m: avg(pick(r3)) * 100, avg12m: avg(a12) * 100,
      win12m: a12.filter((v) => v > 0).length / a12.length * 100,
      n: idx.length,
    };
  });

  // DCA 模擬
  function simulate(budget) {
    const cheap = new Set([4, 5]);
    const u = { V0: 0, V1: 0, V2: 0 }, cash = { V0: 0, V1: 0, V2: 0 };
    const curves = { V0: [], V1: [], V2: [], invested: [] };
    let invested = 0;
    mon.forEach((mm, i) => {
      invested += budget;
      const t = i > 0 ? tierOf(mGaps[i - 1]) : 3;
      u.V0 += budget / mm.price;
      if (cheap.has(t)) { u.V1 += (cash.V1 + budget) / mm.price; cash.V1 = 0; }
      else cash.V1 += budget;
      if (cheap.has(t)) { u.V2 += (cash.V2 + budget * 2) / mm.price; cash.V2 = 0; }
      else cash.V2 += budget;
      curves.V0.push(u.V0 * mm.price); curves.V1.push(u.V1 * mm.price);
      curves.V2.push(u.V2 * mm.price); curves.invested.push(invested);
    });
    return curves;
  }
  function irr(final, nM, budget) {
    let lo = -0.5, hi = 0.5;
    for (let k = 0; k < 200; k++) {
      const mid = (lo + hi) / 2;
      let fv = 0;
      for (let i = 0; i < nM; i++) fv += budget * Math.pow(1 + mid, nM - 1 - i);
      fv < final ? (lo = mid) : (hi = mid);
    }
    return Math.pow(1 + (lo + hi) / 2, 12) - 1;
  }
  const curves = simulate(budget);
  const nM = mon.length, inv = curves.invested[nM - 1];
  const simStats = {};
  ["V0", "V1", "V2"].forEach((k) => {
    const f = curves[k][nM - 1];
    simStats[k] = { final: f, multiple: f / inv, irr: irr(f, nM, budget) * 100 };
  });

  const lastGap = gaps[n - 1];
  return {
    asof: rows[n - 1].date, start: rows[0].date, nDays: n,
    kpi: {
      price: rows[n - 1].price, valuation: rows[n - 1].valuation,
      gapPct: lastGap * 100, percentile: sorted.filter((g) => g < lastGap).length / n * 100,
      tier: tierOf(lastGap),
    },
    bands: { p25: quantile(sorted, 0.25) * 100, p75: quantile(sorted, 0.75) * 100, median: quantile(sorted, 0.5) * 100 },
    quintileEdges: [sorted[0] * 100, ...edges.map((e) => e * 100), sorted[n - 1] * 100],
    quintiles,
    monthly: {
      dates: mon.map((x) => x.date), price: mon.map((x) => x.price),
      valuation: mon.map((x) => x.valuation), gap: mGaps.map((g) => g * 100),
      tiers: mon.map((x) => tierOf(x.gap)),
    },
    curves, simStats, invested: inv, months: nM,
  };
}

/* ---------- KPI ---------- */
const TIER_TXT = { 1: "Q1 最貴", 2: "Q2 偏貴", 3: "Q3 中性", 4: "Q4 偏平", 5: "Q5 最平" };
function renderKPI() {
  const k = D.kpi;
  const set = (id, v) => (document.getElementById(id).textContent = v);
  set("kPrice", fmt$(k.price));
  set("kVal", fmt$(k.valuation));
  set("kAsof", `截至 ${D.asof} · ${D.nDays.toLocaleString()} 日數據`);
  const el = document.getElementById("kGap");
  el.textContent = fmtPct(k.gapPct, 2);
  el.className = "k-value " + (k.gapPct >= 0 ? "pos" : "neg");
  set("kGapNote", k.gapPct >= 0 ? "股價低過估值 · 平" : "股價高過估值 · 貴");
  set("kPct", k.percentile.toFixed(0) + "%");
  set("kPctNote", `p25 ${fmtPct(D.bands.p25)} · p75 ${fmtPct(D.bands.p75)}`);
  set("kTier", "Q" + k.tier);
  const note = document.getElementById("kTierNote");
  note.textContent = TIER_TXT[k.tier];
  note.title = "五分位分界：" + D.quintileEdges.map((e) => e.toFixed(1) + "%").join(" / ");
  document.getElementById("dataMeta").textContent =
    `數據範圍 ${D.start} → ${D.asof} · ${D.nDays.toLocaleString()} 個交易日 · ${D.months} 個月 · 上次重算 ${new Date().toISOString().slice(0, 10)}`;
}

let NEXT_EARN = null;
function renderEarnings(iso) {
  const value = document.getElementById("kEarn");
  const note = document.getElementById("kEarnNote");
  if (!value || !note) return;
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    value.textContent = "—";
    note.textContent = "按「擷取最新數據」取得";
    return;
  }
  value.textContent = iso;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(`${iso}T00:00:00`);
  const days = Math.round((target - today) / 86400000);
  if (days > 1) note.textContent = `還有 ${days} 日 · yfinance`;
  else if (days === 1) note.textContent = "明日公布 · yfinance";
  else if (days === 0) note.textContent = "今日公布 · yfinance";
  else note.textContent = "日期已過，請再擷取";
}
async function loadEarnings() {
  try {
    const res = await authFetch("data/last_eps.json", { cache: "no-store" });
    if (!res.ok) throw new Error("missing");
    const data = await res.json();
    NEXT_EARN = typeof data.nextEarnings === "string" ? data.nextEarnings : null;
  } catch {
    NEXT_EARN = null;
  }
  renderEarnings(NEXT_EARN);
}

/* ---------- 圖表共用 ---------- */
function destroy(id) { if (CH[id]) { CH[id].destroy(); delete CH[id]; } }
const X_TICKS = { autoSkip: true, maxTicksLimit: 11, maxRotation: 0 };

/* 第一課:股價 vs 估值 (log) */
function chart1() {
  destroy("c1");
  CH.c1 = new Chart(document.getElementById("c1"), {
    type: "line",
    data: {
      labels: D.monthly.dates,
      datasets: [
        { label: "股價 Price", data: D.monthly.price, borderColor: C.price, borderWidth: 1.8, pointRadius: 0, tension: 0.15 },
        { label: "估值 Valuation", data: D.monthly.valuation, borderColor: C.val, borderWidth: 1.8, pointRadius: 0, stepped: true },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: X_TICKS, grid: { display: false } },
        y: { type: "logarithmic", ticks: { font: { family: MONO, size: 10.5 }, callback: (v) => "$" + v } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#1c2330", borderColor: "#2b323d", borderWidth: 1, titleFont: { family: MONO },
          callbacks: {
            label: (c) => `${c.dataset.label}: ${fmt$(c.parsed.y)}`,
            footer: (items) => items.length ? `gap: ${fmtPct(D.monthly.gap[items[0].dataIndex], 1)}` : "",
          },
        },
      },
    },
  });
}

/* 第二課:gap 時間序列 + p25/p75 */
function chart2() {
  destroy("c2");
  CH.c2 = new Chart(document.getElementById("c2"), {
    type: "line",
    data: {
      labels: D.monthly.dates,
      datasets: [
        {
          label: "gap %", data: D.monthly.gap, borderWidth: 1.6, pointRadius: 0, tension: 0.2,
          segment: { borderColor: (c) => (c.p1.parsed.y >= 0 ? C.green : C.red) },
          fill: { target: "origin", above: "rgba(63,178,127,.14)", below: "rgba(229,83,75,.14)" },
        },
        { label: "p75", data: D.monthly.dates.map(() => D.bands.p75), borderColor: C.faint, borderDash: [5, 4], borderWidth: 1, pointRadius: 0 },
        { label: "p25", data: D.monthly.dates.map(() => D.bands.p25), borderColor: C.faint, borderDash: [5, 4], borderWidth: 1, pointRadius: 0 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: X_TICKS, grid: { display: false } },
        y: { ticks: { font: { family: MONO, size: 10.5 }, callback: (v) => v + "%" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#1c2330", borderColor: "#2b323d", borderWidth: 1, titleFont: { family: MONO },
          filter: (t) => t.dataset.label === "gap %",
          callbacks: { label: (c) => `gap: ${fmtPct(c.parsed.y, 1)} · 分層 Q${D.monthly.tiers[c.dataIndex]}` },
        },
      },
    },
  });
}

/* 第三課:五分位前瞻回報 */
function chart3() {
  destroy("c3");
  const q = D.quintiles;
  CH.c3 = new Chart(document.getElementById("c3"), {
    type: "bar",
    data: {
      labels: q.map((x) => `Q${x.tier}`),
      datasets: [
        { label: "3 個月平均回報", data: q.map((x) => x.avg3m), backgroundColor: "rgba(76,141,255,.75)", borderRadius: 5, maxBarThickness: 46 },
        { label: "12 個月平均回報", data: q.map((x) => x.avg12m), backgroundColor: "rgba(201,162,39,.8)", borderRadius: 5, maxBarThickness: 46 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { display: false } },
        y: { ticks: { font: { family: MONO, size: 10.5 }, callback: (v) => v + "%" } },
      },
      plugins: {
        legend: { position: "top", align: "end", labels: { boxWidth: 12, boxHeight: 12, borderRadius: 3, useBorderRadius: true } },
        tooltip: {
          backgroundColor: "#1c2330", borderColor: "#2b323d", borderWidth: 1,
          callbacks: {
            title: (items) => {
              const x = q[items[0].dataIndex];
              return `Q${x.tier} · gap ${x.gapMin.toFixed(1)}% ~ ${x.gapMax.toFixed(1)}%`;
            },
            label: (c) => `${c.dataset.label}: ${fmtPct(c.parsed.y, 2)}`,
            footer: (items) => `12M 勝率 ${q[items[0].dataIndex].win12m.toFixed(0)}% · n=${q[items[0].dataIndex].n}`,
          },
        },
      },
    },
  });
}

/* 第四課:策略淨值曲線 */
function chart4(curves, budget) {
  destroy("c4");
  const inv = curves.invested;
  CH.c4 = new Chart(document.getElementById("c4"), {
    type: "line",
    data: {
      labels: D.monthly.dates,
      datasets: [
        { label: "V0 每月照供", data: curves.V0, borderColor: C.blue, borderWidth: 2, pointRadius: 0, tension: 0.15 },
        { label: "V1 平先供(貴持現金)", data: curves.V1, borderColor: C.gold, borderWidth: 1.6, pointRadius: 0, tension: 0.15 },
        { label: "V2 平加倍·貴泊現金", data: curves.V2, borderColor: C.green, borderWidth: 1.6, pointRadius: 0, tension: 0.15 },
        { label: "累計投入", data: inv, borderColor: C.faint, borderDash: [6, 5], borderWidth: 1.2, pointRadius: 0 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: X_TICKS, grid: { display: false } },
        y: { ticks: { font: { family: MONO, size: 10.5 }, callback: (v) => "$" + (v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : (v / 1e3).toFixed(0) + "k") } },
      },
      plugins: {
        legend: { position: "top", align: "end", labels: { boxWidth: 14, boxHeight: 2 } },
        tooltip: {
          backgroundColor: "#1c2330", borderColor: "#2b323d", borderWidth: 1, titleFont: { family: MONO },
          callbacks: { label: (c) => `${c.dataset.label}: $${Math.round(c.parsed.y).toLocaleString()}` },
        },
      },
    },
  });

  // 統計卡
  const n = curves.V0.length;
  const stats = ["V0", "V1", "V2"].map((k) => {
    const f = curves[k][n - 1];
    let lo = -0.5, hi = 0.5;
    for (let it = 0; it < 200; it++) {
      const mid = (lo + hi) / 2;
      let fv = 0;
      for (let i = 0; i < n; i++) fv += budget * Math.pow(1 + mid, n - 1 - i);
      fv < f ? (lo = mid) : (hi = mid);
    }
    return { k, f, mult: f / inv[n - 1], irr: (Math.pow(1 + (lo + hi) / 2, 12) - 1) * 100 };
  });
  const names = { V0: "V0 每月照供", V1: "V1 平先供", V2: "V2 平加倍·貴泊現金" };
  const colors = { V0: C.blue, V1: C.gold, V2: C.green };
  document.getElementById("simStats").innerHTML = stats.map((s) => `
    <div class="sim-stat" style="border-top:3px solid ${colors[s.k]}">
      <h4>${names[s.k]}</h4>
      <div class="v">$${Math.round(s.f).toLocaleString()}</div>
      <div class="d">${s.mult.toFixed(2)}× 投入 · IRR ${s.irr.toFixed(1)}%/年</div>
    </div>`).join("");
}

/* ---------- 表格 ---------- */
function renderTables() {
  // 平貴週期(連續同向段, 月度)
  const g = D.monthly.gap, dts = D.monthly.dates;
  const runs = [];
  let s = 0;
  for (let i = 1; i <= g.length; i++) {
    if (i === g.length || Math.sign(g[i]) !== Math.sign(g[s])) {
      const seg = g.slice(s, i);
      const avg = seg.reduce((a, b) => a + b, 0) / seg.length;
      const months = i - s;
      if (months >= 4) runs.push({ from: dts[s].slice(0, 7), to: dts[i - 1].slice(0, 7), sign: avg >= 0, avg, months, cur: i === g.length });
      s = i;
    }
  }
  document.querySelector("#cycleTable tbody").innerHTML = runs.map((r) => `
    <tr class="${r.cur ? "cur" : ""}">
      <td class="${r.sign ? "pos" : "neg"}">${r.sign ? "平（估值高過股價）" : "貴（股價高過估值）"}</td>
      <td>${r.from} → ${r.to}（${r.months} 個月）</td>
      <td class="num ${r.sign ? "pos" : "neg"}">${fmtPct(r.avg, 1)}</td>
      <td>${r.cur ? "現時進行中" : r.sign ? "之後估值回落／股價上升收斂" : "之後估值追返或股價回調"}</td>
    </tr>`).join("");

  // 五分位表
  document.querySelector("#qTable tbody").innerHTML = D.quintiles.map((q) => `
    <tr class="${q.tier === D.kpi.tier ? "qcur" : ""}">
      <td>Q${q.tier} ${q.tier === D.kpi.tier ? "← 現時" : ""}</td>
      <td class="num">${q.gapMin.toFixed(1)}% ~ ${q.gapMax.toFixed(1)}%</td>
      <td class="num ${q.avg3m >= 0 ? "pos" : "neg"}">${fmtPct(q.avg3m, 2)}</td>
      <td class="num ${q.avg12m >= 0 ? "pos" : "neg"}">${fmtPct(q.avg12m, 2)}</td>
      <td class="num">${q.win12m.toFixed(0)}%</td>
    </tr>`).join("");

  document.getElementById("icNote").textContent =
    `歷史驗證：gap 對 3M 前瞻回報 IC≈0.16、12M IC≈0.33（月末非重疊 IC≈0.18，p<0.01）— 估值差愈平，往後回報中位數愈高，但單月分層訊號以長期平均計。`;
}

/* ---------- 下載 CSV (更新後嘅新基準數據) ---------- */
function downloadCSV() {
  const csv = "date,price,valuation\n" + DAILY.map((r) => `${r.date},${Number(r.price).toFixed(2)},${Number(r.valuation).toFixed(2)}`).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `googl_daily_${D.asof}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  dirty = false;
  document.getElementById("updateStatus").textContent = `已下載基準 CSV（截至 ${D.asof}）`;
}

/* ---------- 更新今日數據 ---------- */
const mask = document.getElementById("updMask");
const dlgStatus = document.getElementById("dlgStatus");
function setStatus(msg, cls = "") {
  dlgStatus.textContent = msg;
  dlgStatus.className = "dlg-status " + cls;
}
function budgetNow() {
  return parseFloat(document.getElementById("budget").value) || 1000;
}
function setBarStatus(msg, cls = "") {
  const el = document.getElementById("updateStatus");
  el.textContent = msg;
  el.className = cls;
}
function applyRow(dateStr, price) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) throw new Error("日期格式要 YYYY-MM-DD");
  if (!isFinite(price) || price <= 0) throw new Error("收市價無效");
  const last = DAILY[DAILY.length - 1];
  if (DAILY.some((r) => r.date === dateStr)) { setStatus(`${dateStr} 已經喺數據入面,無需新增`, ""); return false; }
  if (dateStr <= last.date) throw new Error(`日期 ${dateStr} 早過最新數據 ${last.date}`);
  DAILY.push({ date: dateStr, price: Math.round(price * 100) / 100, valuation: Math.round(last.valuation * 100) / 100 });
  D = computeAll(DAILY, budgetNow());
  renderAll();
  dirty = true;
  document.body.classList.add("has-update");
  setBarStatus(`已手動新增 ${dateStr}（未寫入 CSV）· 記得撳「下載 CSV」`);
  return true;
}
async function reloadCsv() {
  const csvRes = await authFetch("data/googl_daily.csv", { cache: "no-store" });
  if (!csvRes.ok) throw new Error(csvRes.status === 401 ? "PIN 已失效，請重新解鎖" : "CSV 重新載入失敗");
  DAILY = parseCSV(await csvRes.text());
  D = computeAll(DAILY, budgetNow());
  dirty = false;
  document.body.classList.remove("has-update");
  renderAll();
}
function applyUpdate(data) {
  const rows = Array.isArray(data.rows) ? data.rows : [];
  let changed = false;
  for (const row of rows) {
    const price = Number(row.price);
    const valuation = Number(row.valuation);
    if (!row.date || !isFinite(price) || !isFinite(valuation)) continue;
    if (DAILY.some((item) => item.date === row.date)) continue;
    DAILY.push({ date: row.date, price, valuation });
    changed = true;
  }
  if (changed) DAILY.sort((a, b) => (a.date < b.date ? -1 : 1));
  if (!changed && data.valuationUpdated && DAILY.length && isFinite(Number(data.valuation))) {
    DAILY[DAILY.length - 1].valuation = Number(data.valuation);
    changed = true;
  }
  if (
    !changed
    && data.lastRow
    && DAILY.length
    && data.lastRow.date === DAILY[DAILY.length - 1].date
    && isFinite(Number(data.lastRow.valuation))
    && Number(data.lastRow.valuation) !== DAILY[DAILY.length - 1].valuation
  ) {
    DAILY[DAILY.length - 1].valuation = Number(data.lastRow.valuation);
    if (isFinite(Number(data.lastRow.price))) {
      DAILY[DAILY.length - 1].price = Number(data.lastRow.price);
    }
    changed = true;
  }
  if (changed) {
    D = computeAll(DAILY, budgetNow());
    const keepLocal = data.persisted === false || data.ephemeral === true;
    dirty = keepLocal;
    document.body.classList.toggle("has-update", keepLocal);
    renderAll();
  }
  if (typeof data.nextEarnings === "string" && /^\d{4}-\d{2}-\d{2}$/.test(data.nextEarnings)) {
    NEXT_EARN = data.nextEarnings;
    renderEarnings(NEXT_EARN);
  }
  return changed;
}
async function pullLatest() {
  const btn = document.getElementById("btnUpdate");
  btn.disabled = true;
  setBarStatus("正在向 yfinance 擷取最新數據…");
  try {
    const res = await authFetch("/api/update", { method: "POST", cache: "no-store" });
    let data = {};
    try { data = await res.json(); } catch { /* 非 JSON 回應 */ }
    if (res.status === 401) throw new Error(data.message || "需要 PIN 先可更新數據");
    if (!res.ok || !data.ok) throw new Error(data.message || `伺服器回應 ${res.status}`);
    applyUpdate(data);
    if (!(typeof data.nextEarnings === "string" && data.nextEarnings)) await loadEarnings();
    setBarStatus(data.message, "ok");
  } catch (err) {
    const offline = /Failed to fetch|NetworkError|Load failed/i.test(err.message);
    setBarStatus(offline
      ? "擷取失敗：請用 python server.py 開啟頁面。每次撳掣先會向 yfinance 要一次。"
      : "擷取失敗：" + err.message, "err");
  } finally {
    btn.disabled = false;
  }
}

mask.addEventListener("click", (e) => { if (e.target === mask) mask.hidden = true; });
document.getElementById("dlgClose").addEventListener("click", () => { mask.hidden = true; });
document.getElementById("btnUpdate").addEventListener("click", pullLatest);
document.getElementById("btnManualOpen").addEventListener("click", () => {
  document.getElementById("manualDate").value = new Date().toISOString().slice(0, 10);
  setStatus("輸入日期同收市價。估值沿用上一行，呢步唔會寫入 CSV 檔。");
  mask.hidden = false;
});
document.getElementById("btnManual").addEventListener("click", () => {
  try {
    const d = document.getElementById("manualDate").value;
    const p = parseFloat(document.getElementById("manualPrice").value);
    if (applyRow(d, p)) setStatus(`已手動新增 ${d} @ $${p.toFixed(2)} · 估值沿用上次`, "ok");
  } catch (err) {
    setStatus(err.message, "err");
  }
});
document.getElementById("btnCsv").addEventListener("click", downloadCSV);

/* ---------- 啟動 ---------- */
function renderAll() {
  renderKPI();
  chart1(); chart2(); chart3();
  chart4(D.curves, parseFloat(document.getElementById("budget").value) || 1000);
  renderTables();
}

function showApp() {
  document.body.classList.remove("locked");
  const gate = document.getElementById("pinGate");
  const shell = document.getElementById("appShell");
  if (gate) gate.hidden = true;
  if (shell) shell.hidden = false;
}

function showPinGate(message) {
  document.body.classList.add("locked");
  const gate = document.getElementById("pinGate");
  const shell = document.getElementById("appShell");
  if (gate) gate.hidden = false;
  if (shell) shell.hidden = true;
  const status = document.getElementById("pinStatus");
  if (status && message) {
    status.textContent = message;
    status.className = "pin-status";
  }
}

async function unlockWithPin(pin) {
  const res = await authFetch("/api/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin }),
  });
  let data = {};
  try { data = await res.json(); } catch { /* ignore */ }
  if (res.status === 503) throw new Error(data.message || "伺服器未設定存取 PIN");
  if (res.status === 429) throw new Error(data.message || "嘗試次數過多，請稍後再試");
  if (!res.ok || !data.ok) throw new Error(data.message || "PIN 不正確");
  accessPin = pin;
  sessionStorage.setItem("googl_pin", pin);
  return data;
}

async function boot() {
  try {
    const res = await authFetch("data/googl_daily.csv", { cache: "no-store" });
    if (res.status === 401) throw new Error("PIN_REQUIRED");
    if (!res.ok) throw new Error("讀不到 CSV（" + res.status + "）");
    DAILY = parseCSV(await res.text());
    D = computeAll(DAILY, budgetNow());
    showApp();
    renderAll();
    await loadEarnings();
  } catch (err) {
    if (err.message === "PIN_REQUIRED") {
      showPinGate(accessPin ? "PIN 已失效，請重新輸入" : "");
      return;
    }
    setBarStatus("頁面啟動失敗：" + err.message, "err");
    showApp();
  }
}

async function start() {
  const form = document.getElementById("pinForm");
  const input = document.getElementById("pinInput");
  const status = document.getElementById("pinStatus");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pin = (input.value || "").trim();
      status.textContent = "核對中…";
      status.className = "pin-status";
      try {
        await unlockWithPin(pin);
        status.textContent = "已解鎖";
        status.className = "pin-status ok";
        input.value = "";
        await boot();
      } catch (err) {
        status.textContent = err.message || "PIN 不正確";
        status.className = "pin-status";
      }
    });
  }
  if (accessPin) {
    try {
      await unlockWithPin(accessPin);
      await boot();
      return;
    } catch {
      sessionStorage.removeItem("googl_pin");
      accessPin = "";
    }
  }
  showPinGate("");
}

document.getElementById("csvFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    try {
      DAILY = parseCSV(rd.result);
      D = computeAll(DAILY, budgetNow());
      dirty = false;
      document.getElementById("updateStatus").textContent = "";
      renderAll();
    } catch (err) {
      alert("CSV 讀取失敗：" + err.message);
    }
  };
  rd.readAsText(f);
});
document.getElementById("btnSim").addEventListener("click", () => {
  D = computeAll(DAILY, budgetNow());
  chart4(D.curves, budgetNow());
});

window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});

start();
