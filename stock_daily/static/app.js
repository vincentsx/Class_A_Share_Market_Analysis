"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (value, digits = 2) => value === null || value === undefined ? "—" : Number(value).toFixed(digits);
const pct = (value) => value === null || value === undefined ? "—" : `${value > 0 ? "+" : ""}${num(value)}%`;
const color = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "muted";
const dateLabel = (day) => day ? `${day.slice(0,4)}.${day.slice(4,6)}.${day.slice(6,8)}` : "—";
const timeLabel = (value) => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value)) : "—";
const emptyRow = (text, columns) => `<tr><td colspan="${columns}" class="empty-row">${esc(text)}</td></tr>`;
let state = null, strategy = "all", currentView = "overview", refreshId = 0;
const offlineElement = document.getElementById("offline-snapshot");
const OFFLINE = offlineElement ? JSON.parse(offlineElement.textContent) : null;

if (OFFLINE) {
  $("add-watch").hidden = true;
  $("date-select").disabled = true;
  $("refresh").textContent = "↻ 重载文件";
}

async function api(path, options = {}) {
  if (OFFLINE) {
    if (options.method && options.method !== "GET") throw new Error("离线报告为只读快照，请在本地服务中管理自选股。");
    const url = new URL(path, "https://snapshot.invalid");
    if (url.pathname === "/api/dashboard") return structuredClone(OFFLINE.dashboard);
    if (url.pathname === "/api/tracking") return structuredClone(OFFLINE.tracking);
    const match = url.pathname.match(/^\/api\/stocks\/([^/]+)\/history$/);
    if (match) return structuredClone(OFFLINE.histories[decodeURIComponent(match[1])] || []);
    throw new Error("离线快照中没有该数据。");
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { const body = await response.json(); if (typeof body.detail === "string") detail = body.detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}
function message(text, isError = false) {
  $("message").textContent = text;
  $("message").className = `notice${isError ? " error" : ""}`;
  $("message").hidden = !text;
}
async function load(day = "") {
  const id = ++refreshId;
  $("refresh").disabled = true;
  try {
    const data = await api(`/api/dashboard${day ? `?day=${encodeURIComponent(day)}` : ""}`);
    if (id !== refreshId) return;
    state = data;
    $("demo-banner").hidden = data.source !== "demo";
    $("empty").hidden = !!data.report;
    $("report-content").hidden = !data.report;
    $("date-select").innerHTML = data.dates.length ? data.dates.map((d) => `<option value="${d}">${dateLabel(d)} 复盘</option>`).join("") : '<option value="">暂无报告</option>';
    if (data.report) {
      $("date-select").value = data.report.day;
      const older = data.report.day !== (OFFLINE ? new Date().toLocaleDateString("sv-SE", {timeZone:"Asia/Shanghai"}).replaceAll("-", "") : data.today);
      const recentTask = data.runs.find((r) => r.kind === "daily" && r.status !== "skipped" && r.day >= data.report.day);
      const recentFailure = recentTask?.status === "failed" ? recentTask : null;
      const reportNotice = recentFailure ? `最近采集失败：${recentFailure.message}。当前显示 ${dateLabel(data.report.day)} 的已保存报告。` : older ? `当前显示 ${dateLabel(data.report.day)} 的历史报告，数据截止于该日。` : "";
      message([reportNotice, ...(data.report.data_notes || [])].filter(Boolean).join(" "), !!recentFailure);
      renderReport(data);
      renderWatch(data.watchlist);
    } else {
      const failure = data.runs.find((r) => r.kind === "daily" && r.status === "failed");
      const active = data.runs.find((r) => r.kind === "daily" && r.status === "running");
      const progress = data.source === "baostock" && data.factor_histories ? `；已完成 ${data.factor_histories} 只股票的历史复权核对` : "";
      $("empty-title").textContent = active ? "正在采集，报告尚未发布。" : failure ? "采集失败，尚未生成报告。" : "第一份复盘，等待生成。";
      $("empty-help").textContent = active ? (OFFLINE ? "这是导出时的采集状态；任务成功后需重新导出并打开文件。" : "完成历史补齐和数据校验后将显示报告，请勿重复启动任务。") : failure ? "进程结束不代表采集成功。已保存的历史可复用，修复接口问题后执行下方命令继续；成功后重新打开导出的HTML。" : data.source === "demo" ? "初始化合成示例后即可浏览。" : "请运行盘后采集。非交易日用 --date 指定实际交易日，首次需要补齐历史。";
      const task = active || failure;
      $("empty-command").textContent = `.venv/bin/python -m stock_daily ${data.source === "demo" ? "demo" : "run"}${task ? ` --date ${task.day}` : ""}`;
      $("empty-command").hidden = !!active;
      message(active ? `${OFFLINE ? "导出时正在采集" : "正在采集"} ${dateLabel(active.day)} 的${data.source === "demo" ? "合成示例" : "真实行情"}：${active.message}${progress}。${OFFLINE ? "本文件尚无已发布报告，采集完成后需重新导出。" : "完成数据校验后将显示报告。"}` : failure ? `最近任务失败：${failure.message}` : "", !active && !!failure);
    }
    if (currentView === "tracking") await loadTracking();
  } catch (error) { message(error.message, true); }
  finally { if (id === refreshId) $("refresh").disabled = false; }
}

function renderReport(data) {
  const report = data.report, m = report.market, q = report.quality;
  $("market-date").textContent = `${dateLabel(report.day)} · ${m.count} 只当日有行情股票${data.source === "baostock" ? " · 沪深A股，不含北交所" : ""}`;
  const cards = [
    ["沪深300", pct(m.benchmark_pct), color(m.benchmark_pct), `近20日 ${pct(m.benchmark_return20)}`],
    ["上涨股票占比", `${num(m.up_ratio,1)}<small>%</small>`, "", `${m.up} 上涨 / ${m.down} 下跌 / ${m.flat} 平盘`],
    ["全市场成交额", `${num(m.amount / 1e8,0)}<small>亿元</small>`, "", `此前20日均值的 ${num(m.amount_ratio20)} 倍`],
    ["站上20日均线", `${num(m.above_ma20_ratio,1)}<small>%</small>`, "", `${m.eligible_history_count} 只连续历史样本`],
  ];
  $("market-cards").innerHTML = cards.map(([title,value,cls,sub]) => `<div class="metric-card"><div class="metric-title">${title}<span>↗</span></div><div class="metric-value ${cls}">${value}</div><div class="metric-subtitle">${esc(sub)}</div></div>`).join("");
  const sectors = report.industries.slice(0,8), max = Math.max(1, ...sectors.map((s) => Math.abs(s.relative20)));
  $("sector-chart").innerHTML = sectors.length ? sectors.map((s) => {
    const width = Math.max(1, Math.abs(s.relative20) / max * 140);
    const x = s.relative20 >= 0 ? 145 : 145 - width;
    return `<div class="sector-row"><span>${esc(s.name)}<small>${s.count}</small></span><svg viewBox="0 0 290 18" role="img" aria-label="${esc(s.name)}相对强度${num(s.relative20)}个百分点"><rect x="0" y="5" width="290" height="8" rx="3" fill="#f5f7f2"/><line x1="145" y1="2" x2="145" y2="16" stroke="#d9e1d7"/><rect x="${num(x)}" y="5" width="${num(width)}" height="8" rx="2" fill="${s.relative20 >= 0 ? '#91ad83' : '#a5b8bd'}"/></svg><span class="sector-value ${color(s.relative20)}">${s.relative20 > 0 ? "+" : ""}${num(s.relative20)}</span></div>`;
  }).join("") : '<p class="muted">连续历史样本不足。</p>';
  $("quality-chip").textContent = q.status === "complete" ? "行情校验通过" : "部分行情缺失";
  $("quality-chip").className = `chip${q.status !== "complete" ? " amber" : ""}`;
  const eventText = {demo:"合成示例公告",available:"已采集，仅核对标题",disabled:"未启用，风险未核验",unavailable:"接口不可用，风险未核验"}[report.event_status] || "未知";
  $("quality-content").innerHTML = [
    ["应有行情 / 实际采集", `${q.expected} / ${q.collected}`], ["行情覆盖率", `${num(q.coverage,1)}%`],
    ["停牌 / 缺失", `${q.suspended.length} / ${q.missing.length}`], ["公告检查", eventText],
    ["采集时间", timeLabel(report.acquired_at)],
  ].map(([label,value]) => `<div class="quality-line"><span>${label}</span><b>${esc(value)}</b></div>`).join("");
  $("schedule-time").textContent = data.schedule_time;
  $("candidate-count").textContent = report.candidates.length;
  if (OFFLINE) {
    $("download").href = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)], {type:"application/json"}));
    $("download").download = `${data.source}-${report.day}.json`;
  } else $("download").href = `/api/report/${report.day}/download`;
  renderCandidates();
  const events = report.events.filter((e) => e.tags.length).slice(0,8);
  $("event-list").innerHTML = events.length ? events.map((e) => `<div class="list-entry">${eventLink(e)}<p>${esc(e.code)} · ${dateLabel(e.day)} · ${esc(e.tags.join(" / "))}</p></div>`).join("") : `<div class="list-entry muted">${report.event_status === "available" ? "已采集公告中暂无命中关键词的标题；不代表没有风险。" : "公告未启用或不可用，暂无法完成事件风险核验。"}</div>`;
  const labels = {success:"成功",failed:"失败",running:"运行中",skipped:"跳过"};
  $("run-list").innerHTML = data.runs.slice(0,4).map((r) => `<div class="list-entry"><div class="run-title"><span class="run-state ${esc(r.status)}">${labels[r.status] || esc(r.status)}</span><span>${r.kind === "daily" ? "盘后分析" : "公告补查"}</span><span class="muted">${timeLabel(r.started_at)}</span></div><p>${esc(r.message)}</p></div>`).join("") || '<div class="list-entry muted">暂无任务记录</div>';
}
function eventLink(event) {
  let url;
  try { url = new URL(event.url); } catch (_) { return esc(event.title); }
  return ["https:","http:"].includes(url.protocol) ? `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(event.title)} ↗</a>` : esc(event.title);
}
function renderCandidates() {
  if (!state?.report) return;
  const search = $("candidate-search").value.trim().toLowerCase();
  const candidates = state.report.candidates.filter((c) => (strategy === "all" || c.strategy === strategy) && `${c.name} ${c.code} ${c.industry}`.toLowerCase().includes(search));
  $("candidate-table").innerHTML = candidates.length ? candidates.map((c) => `<tr><td><b>${esc(c.name)}</b><small>${esc(c.code)}</small></td><td><span class="signal-tag ${esc(c.strategy)}">${esc(c.label)}</span></td><td>${num(c.close)}<small class="${color(c.pct)}">${pct(c.pct)}</small></td><td class="${color(c.relative20)}">${c.relative20 > 0 ? "+" : ""}${num(c.relative20)} pp</td><td>${num(c.volume_ratio)} ×</td><td class="muted">${esc(c.industry)}</td><td><button class="row-detail" data-code="${esc(c.code)}" data-rule="${esc(c.strategy)}">查看依据 ↗</button></td></tr>`).join("") : emptyRow(state.report.candidates.length ? "没有符合当前搜索条件的信号" : "今天没有满足规则的观察信号；保持筛选标准，等待新的变化。",7);
}
function renderWatch(items) {
  $("watch-table").innerHTML = items.length ? items.map((w) => `<tr><td><b>${esc(w.name || w.code)}</b><small>${esc(w.code)}</small><small>${esc(w.note)}</small></td><td>${num(w.close)}<small class="${color(w.pct)}">${pct(w.pct)}</small></td><td>${w.available ? esc(w.trend) : "历史不足"}</td><td><ul class="alert-list">${w.alerts.length ? w.alerts.map((a) => `<li>${esc(a)}</li>`).join("") : '<li class="quiet">当前检查项未触发提醒</li>'}</ul></td><td>${OFFLINE ? "" : `<button class="remove-watch" data-code="${esc(w.code)}">移除</button>`}</td></tr>`).join("") : emptyRow("添加自选股，持续观察你关注的变化。",5);
}
async function showDetail(code, rule) {
  const candidate = state.report.candidates.find((c) => c.code === code && c.strategy === rule);
  if (!candidate) return;
  $("detail-code").textContent = `${candidate.code} · ${dateLabel(state.report.day)} · ${candidate.label}`;
  $("detail-title").textContent = candidate.name;
  $("detail-body").innerHTML = `<div class="dialog-metrics"><div><span>收盘价</span><b>${num(candidate.close)}</b></div><div><span>20日相对强度</span><b class="${color(candidate.relative20)}">${num(candidate.relative20)} pp</b></div><div><span>成交量倍数</span><b>${num(candidate.volume_ratio)}×</b></div><div><span>ATR / 收盘</span><b>${num(candidate.atr14 / candidate.close * 100)}%</b></div></div><div id="detail-chart" class="muted">正在加载历史走势…</div><div class="detail-block"><h3>入选依据</h3><ul>${candidate.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul><h3>后续观察</h3><p class="muted">${esc(candidate.observation)}</p><p class="detail-exit">${esc(candidate.exit_condition)}</p><h3>风险与数据限制</h3><ul>${candidate.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`;
  $("detail-dialog").showModal();
  try {
    const rows = await api(`/api/stocks/${code}/history?day=${state.report.day}`);
    if (!rows.length) { $("detail-chart").textContent = "暂无历史走势"; return; }
    const factor = rows[rows.length-1].adj;
    const values = rows.map((r) => r.close * r.adj / factor), min = Math.min(...values), max = Math.max(...values), spread = max-min || 1;
    const points = values.map((v,i) => `${num(20+i/(values.length-1||1)*560)},${num(140-(v-min)/spread*110)}`).join(" ");
    $("detail-chart").innerHTML = `<svg class="price-chart" viewBox="0 0 600 165" role="img" aria-label="最近65日复权收盘走势"><line class="chart-grid" x1="20" y1="30" x2="580" y2="30"/><line class="chart-grid" x1="20" y1="85" x2="580" y2="85"/><line class="chart-grid" x1="20" y1="140" x2="580" y2="140"/><polyline class="chart-line" points="${points}"/></svg><div class="chart-caption"><span>${dateLabel(rows[0].day)} → ${dateLabel(rows[rows.length-1].day)}</span><span>复权收盘 · 当前价格尺度</span></div>`;
  } catch (error) { if ($("detail-chart")) $("detail-chart").textContent = error.message; }
}
async function loadTracking() {
  try {
    const data = await api("/api/tracking");
    $("tracking-method").textContent = data.method;
    $("tracking-cards").innerHTML = data.summary.map((s) => `<div class="tracking-card"><h3>${s.horizon}个交易日 · 平均收盘变化</h3><div class="metric-value ${color(s.mean_return)}">${pct(s.mean_return)}</div><p>${s.count} 个完整观察 · 正变化占比 ${num(s.positive_ratio,1)}%</p><p>平均期间最大不利变化 ${pct(s.mean_adverse)}</p></div>`).join("");
    const horizon = (item,n) => { const h = item.horizons[String(n)]; return h?.status === "ready" ? `<span class="${color(h.return)}">${pct(h.return)}</span>` : `<span class="muted">${h?.status === "missing" ? "数据缺失" : "观察中"}</span>`; };
    $("tracking-table").innerHTML = data.items.length ? data.items.map((item) => `<tr><td><b>${esc(item.name)}</b><small>${dateLabel(item.day)} · ${esc(item.code)}</small></td><td><span class="signal-tag ${esc(item.strategy)}">${esc(item.label)}</span></td><td>${horizon(item,5)}</td><td>${horizon(item,10)}</td><td>${horizon(item,20)}</td><td class="muted">${item.invalidated_on ? dateLabel(item.invalidated_on) : "尚无收盘失效记录"}</td></tr>`).join("") : emptyRow("信号发布后，系统会逐日积累观察记录。",6);
  } catch (error) { message(error.message,true); }
}
function switchView(view) {
  currentView = view;
  const text = {overview:["每日复盘","把今天的市场，看清楚。","市场广度、行业强弱与值得跟踪的变化。"],watch:["我的自选","持续关注，你在意的变化。","趋势检查与公告提醒，保留每一次观察的依据。"],tracking:["信号跟踪","让判断，经得起时间检验。","记录信号发布后的变化，逐步积累真实的观察样本。"]}[view];
  $("breadcrumb").textContent = text[0]; $("page-title").textContent = text[1]; $("subtitle").textContent = text[2];
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active",b.dataset.view===view));
  document.querySelectorAll(".view").forEach((s) => s.hidden = s.id!==`view-${view}`);
  if (view === "tracking") loadTracking();
}
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click",() => switchView(b.dataset.view)));
document.querySelectorAll("[data-strategy]").forEach((b) => b.addEventListener("click",() => {strategy=b.dataset.strategy;document.querySelectorAll("[data-strategy]").forEach((s) => s.classList.toggle("active",s===b));renderCandidates();}));
$("candidate-search").addEventListener("input",renderCandidates);
$("candidate-table").addEventListener("click",(e) => {const b=e.target.closest(".row-detail");if(b)showDetail(b.dataset.code,b.dataset.rule);});
$("date-select").addEventListener("change",() => load($("date-select").value));
$("refresh").addEventListener("click",() => OFFLINE ? window.location.reload() : load());
$("method-button").addEventListener("click",() => $("method-dialog").showModal());
$("mobile-method").addEventListener("click",() => $("method-dialog").showModal());
$("add-watch").addEventListener("click",() => {$("watch-error").textContent="";$("watch-dialog").showModal();});
document.querySelectorAll(".close-dialog").forEach((b) => b.addEventListener("click",() => b.closest("dialog").close()));
document.querySelectorAll("dialog").forEach((d) => d.addEventListener("click",(e) => {if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}}));
$("watch-form")?.addEventListener("submit",async (e) => {
  e.preventDefault(); const form=e.target, button=form.querySelector("button[type=submit]"), fields=new FormData(form);button.disabled=true;
  try {await api("/api/watchlist",{method:"POST",headers:{"Content-Type":"application/json","Authorization":`Bearer ${fields.get("key")}`},body:JSON.stringify({code:String(fields.get("code")).trim().toUpperCase(),note:fields.get("note")})});form.reset();$("watch-dialog").close();await load(state.report.day);}
  catch(error){$("watch-error").textContent=error.message;}finally{button.disabled=false;}
});
$("watch-table").addEventListener("click",async (e) => {const b=e.target.closest(".remove-watch");if(!b)return;const key=window.prompt(`移除 ${b.dataset.code}：请输入 APP_API_KEY`);if(!key)return;try{await api(`/api/watchlist/${b.dataset.code}`,{method:"DELETE",headers:{Authorization:`Bearer ${key}`}});await load(state.report.day);}catch(error){message(error.message,true);}});
load();
setInterval(() => {
  if (!OFFLINE && state && !state.report && state.runs.some((r) => r.kind === "daily" && r.status === "running")) load();
}, 15000);
