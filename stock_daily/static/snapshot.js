"use strict";
const data = JSON.parse(document.getElementById("quote-snapshot").textContent);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, digits=2) => Number(v).toLocaleString("zh-CN", {minimumFractionDigits:digits, maximumFractionDigits:digits});
const pct = v => `${v > 0 ? "+" : ""}${num(v)}%`;
const color = v => v > 0 ? "red" : v < 0 ? "green" : "";
const m = data.market;
const cards = [
  ["沪深300", pct(m.benchmark.pct), color(m.benchmark.pct), `最新 ${num(m.benchmark.price)} · ${m.benchmark.quote_time.slice(11,19)}`],
  ["上涨股票占比", `${num(m.up/m.count*100,1)}%`, "", `${m.up} 上涨 / ${m.down} 下跌 / ${m.flat} 平盘`],
  [data.slot === "midday" ? "上午累计成交额" : "当日累计成交额", `${num(m.amount/1e8,0)} 亿`, "", "有效报价样本 · 未外推全天成交额"],
  ["有效报价", num(m.count,0), "", `缓存范围 ${data.expected} 只 · 覆盖 ${num(data.coverage)}%`]
];
document.getElementById("metrics").innerHTML = cards.map(([label,value,cls,sub]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value ${cls}">${esc(value)}</div><p>${esc(sub)}</p></div>`).join("");
document.getElementById("sectors").innerHTML = data.industries.slice(0,12).map(s => `<div class="sector"><div>${esc(s.name)}<small>${s.count} 只 · 上涨 ${num(s.up_ratio,0)}%</small></div><meter class="bar" min="0" max="100" value="${Math.min(100,Math.abs(s.pct)*12)}"></meter><strong class="${color(s.pct)}">${pct(s.pct)}</strong></div>`).join("");
function row(r, focus=false) { return `<tr><td><b>${esc(r.name)}</b><small>${esc(r.code)}</small></td><td>${esc(r.industry)}${focus?`<small>${esc(r.reason)}</small>`:""}</td><td>${num(r.price)}</td><td class="${color(r.pct)}">${pct(r.pct)}</td><td>${num(r.amount/1e8)} 亿</td><td>${esc(r.quote_time.slice(11,19))}</td></tr>`; }
document.getElementById("focus").innerHTML = data.focus.map(r => row(r,true)).join("") || '<tr><td colspan="6">暂无自选股或更早日线信号的有效报价。</td></tr>';
function renderQuotes() {
  const query = document.getElementById("search").value.trim().toLowerCase();
  const matches = data.quotes.filter(r => !query || `${r.code} ${r.name} ${r.industry}`.toLowerCase().includes(query));
  document.getElementById("quotes").innerHTML = matches.slice(0,40).map(r => row(r)).join("") || '<tr><td colspan="6">没有匹配的有效报价。</td></tr>';
  document.getElementById("count").textContent = `匹配 ${matches.length} 只，当前展示前 ${Math.min(40,matches.length)} 只。`;
}
document.getElementById("search").addEventListener("input", renderQuotes);
renderQuotes();
window.REPORT_READY = true;
