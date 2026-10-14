"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (value, digits = 2) => value === null || value === undefined ? "—" : Number(value).toFixed(digits);
const pct = (value) => value === null || value === undefined ? "—" : `${value > 0 ? "+" : ""}${num(value)}%`;
const color = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "muted";
const dateLabel = (day) => day ? `${day.slice(0,4)}.${day.slice(4,6)}.${day.slice(6,8)}` : "—";
const timeLabel = (value) => !value ? "—" : Number.isNaN(new Date(value).getTime()) ? "时间待核对" : new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value));
const emptyRow = (text, columns) => `<tr><td colspan="${columns}" class="empty-row">${esc(text)}</td></tr>`;
let state = null, strategy = "all", currentView = "overview", refreshId = 0, selectedDay = "", trackingData = null, aiSummaryPending = false;
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
    if (url.pathname === "/api/backtest") return structuredClone(OFFLINE.backtest);
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
async function load(day = selectedDay) {
  const id = ++refreshId;
  $("refresh").disabled = true;
  try {
    const data = await api(`/api/dashboard${day ? `?day=${encodeURIComponent(day)}` : ""}`);
    if (id !== refreshId) return;
    state = data;
    renderOperations(data.health);
    const emailStatus = $("email-status");
    if (emailStatus) {
      const mail = data.email;
      emailStatus.hidden = !mail?.enabled;
      if (mail?.enabled) {
        const errors = mail.configuration_errors || [];
        const counts = (mail.items || []).reduce((result, item) => { result[item.status] = (result[item.status] || 0) + 1; return result; }, {});
        emailStatus.hidden = !errors.length && !counts.pending && !counts.sending && !counts.failed;
        emailStatus.textContent = errors.length ? `报告邮件尚未配置完整：${errors.join("、")}。请在本地.env填写；分析结果仍正常保存。` : `报告邮件已开启：最近30封中 ${counts.sent || 0} 封获SMTP接收、${(counts.pending || 0) + (counts.sending || 0)} 封待发、${counts.failed || 0} 封失败。发送记录见任务列表，详情可运行 mail-status 查看。`;
      }
    }
    $("demo-banner").hidden = data.source !== "demo";
    $("empty").hidden = !!data.report;
    $("report-content").hidden = !data.report;
    $("date-select").innerHTML = data.dates.length ? `${OFFLINE ? "" : '<option value="">跟随最新报告</option>'}` + data.dates.map((d) => `<option value="${d}">${dateLabel(d)} 复盘</option>`).join("") : '<option value="">暂无报告</option>';
    if (data.report) {
      $("date-select").value = OFFLINE ? data.report.day : selectedDay;
      const older = data.report.day !== (OFFLINE ? new Date().toLocaleDateString("sv-SE", {timeZone:"Asia/Shanghai"}).replaceAll("-", "") : data.today);
      const recentTask = data.runs.find((r) => r.kind === "daily" && r.status !== "skipped" && r.day >= data.report.day);
      const recentFailure = recentTask?.status === "failed" ? recentTask : null;
      const reportNotice = recentFailure ? `最近采集失败：${recentFailure.message}。当前显示 ${dateLabel(data.report.day)} 的已保存报告。` : older ? (OFFLINE?.portfolio || (!selectedDay && currentView === "backtest") ? `日线分析截至 ${dateLabel(data.report.day)}；持仓估值的行情时间见下方标注。` : `当前显示 ${dateLabel(data.report.day)} 的历史报告，数据截止于该日。`) : "";
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
    if (currentView === "backtest") await loadBacktest();
    if (OFFLINE && $("static-report")) $("static-report").hidden = true;
    window.REPORT_READY = true;
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
  $("schedule-time").textContent = `${data.midday_time || "11:35"} / ${data.schedule_time} / ${data.announcement_time}`;
  $("candidate-count").textContent = report.candidates.length;
  if (OFFLINE) {
    $("download").href = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)], {type:"application/json"}));
    $("download").download = `${data.source}-${report.day}.json`;
  } else $("download").href = `/api/report/${report.day}/download`;
  renderCandidates();
  renderChanges(report.changes);
  renderEvidence();
  renderAISummary(data.evidence_summary);
  if ($("ai-summary-button")) {
    $("ai-summary-button").disabled = !data.ai_configured || aiSummaryPending;
    $("ai-summary-button").title = data.ai_configured ? "手动发送已留存的公开证据到模型，生成带引用摘要" : "在本地配置AI_API_KEY、AI_MODEL和接口后启用";
  }
  const events = report.events.filter((e) => e.tags.length).slice(0,8);
  $("event-list").innerHTML = events.length ? events.map((e) => `<div class="list-entry">${eventLink(e)}<p>${esc(e.code)} · ${dateLabel(e.day)} · ${esc(e.tags.join(" / "))}</p></div>`).join("") : `<div class="list-entry muted">${report.event_status === "available" ? "已采集公告中暂无命中关键词的标题；不代表没有风险。" : "公告未启用或不可用，暂无法完成事件风险核验。"}</div>`;
  const labels = {success:"成功",failed:"失败",running:"运行中",skipped:"跳过"};
  const kinds = {daily:"日线分析", announcements:"公告补查", midday:"午间快照", close:"收盘快照", evening:"晚间分析", email_queue:"邮件排队", email:"报告发信"};
  $("run-list").innerHTML = data.runs.slice(0,4).map((r) => `<div class="list-entry"><div class="run-title"><span class="run-state ${esc(r.status)}">${labels[r.status] || esc(r.status)}</span><span>${esc(kinds[r.kind] || r.kind)}</span><span class="muted">${timeLabel(r.started_at)}</span></div><p>${esc(r.message)}</p></div>`).join("") || '<div class="list-entry muted">暂无任务记录</div>';
}
function eventLink(event) {
  let url;
  try { url = new URL(event.url); } catch (_) { return esc(event.title); }
  return ["https:","http:"].includes(url.protocol) ? `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(event.title)} ↗</a>` : esc(event.title);
}
function renderOperations(data) {
  if (!$("operations-content")) return;
  if (!data) { $("operations-panel").hidden = true; return; }
  const labels = {running:"运行中", stopped:"已停止", stale:"心跳已过期", unknown:"尚未记录心跳"};
  $("worker-chip").textContent = labels[data.worker.status] || data.worker.status;
  $("worker-chip").className = `chip${data.worker.status === "running" ? "" : " amber"}`;
  const stages = {success:"完成", failed:"失败", running:"运行中", skipped:"跳过", not_run:"尚无记录"};
  $("operations-content").innerHTML = `<p>状态检查 ${timeLabel(data.checked_at)} · worker心跳 ${timeLabel(data.worker.at)} · ${esc(data.worker.phase || "尚无记录；请启动或重启worker")}${OFFLINE ? " · 此处为导出时的状态" : ""}</p><p>最新日线 ${dateLabel(data.latest_daily)} · 最新快照 ${dateLabel(data.latest_snapshot?.day)} ${esc(data.latest_snapshot?.slot || "")}</p><div class="research-grid">${data.next_tasks.map((t) => `<div class="research-card"><b>${esc({midday:"午间",close:"收盘",evening:"晚间"}[t.kind])} ${timeLabel(t.at)}</b><p>${t.calendar_verified ? "已保存交易日历中的下一时段" : "下一次时段检查，交易日历待核验"}</p></div>`).join("")}</div><div class="research-grid">${data.stages.map((s) => `<div class="research-card"><b>${esc(s.label)} · ${esc(stages[s.status] || s.status)}</b><p>${timeLabel(s.started_at)} · ${esc(s.message || "")}</p></div>`).join("")}</div><p>数据库备份：${data.backup_enabled ? esc({success:"成功",failed:"失败"}[data.backup.status] || "等待首次备份") : "未启用"} · ${timeLabel(data.backup.at)} ${esc(data.backup.file || "")}</p><p>盘中持仓估值：每${data.portfolio?.interval_minutes || 10}分钟 · ${esc({success:"已采集",failed:"采集失败",skipped:"非交易日",running:"采集中"}[data.portfolio?.status] || "等待交易时段")} · ${timeLabel(data.portfolio?.at)}</p>${data.pages.status ? `<p>公开网站：${esc(data.pages.status)} · ${timeLabel(data.pages.at)}；已触发上传不等于部署完成。</p>` : ""}`;
}
function renderChanges(data) {
  if (!data) { $("daily-changes").textContent = "当前快照尚未保存变化分析。"; return; }
  const groups = [["new","新增"],["continuing","持续满足"],["left","不再入选"],["invalidated","触发原失效条件"],["unavailable","无法判定"]];
  $("daily-changes").innerHTML = `<ul>${(data.summaries || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ul><div class="research-grid">${groups.map(([key,label]) => `<details class="research-card"><summary>${label} · ${(data[key] || []).length} 条</summary>${(data[key] || []).map((c) => `<p><b>${esc(c.name)}</b> ${esc(c.code)} · ${esc(c.label)}<small>${esc(c.change)}</small></p>`).join("") || '<p class="muted">暂无</p>'}</details>`).join("")}</div>${(data.intraday || []).map((p) => `<details><summary>${p.slot === "midday" ? "午间" : "收盘"}观察回顾 · ${timeLabel(p.captured_at)}</summary><ul>${p.summaries.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></details>`).join("")}`;
}
function renderEvidence() {
  const data = state?.report?.evidence;
  const query = $("evidence-search").value.trim().toLowerCase();
  const items = (data?.items || []).filter((c) => `${c.title} ${c.codes.join(" ")} ${c.sources.map((s) => s.name).join(" ")}`.toLowerCase().includes(query));
  const card = (c) => `<article id="evidence-${esc(c.id)}" class="research-card"><span class="chip">${esc(c.kind)}</span><h3>${esc(c.title)}</h3><p>${esc(c.summary)}</p><p class="muted">发布 ${timeLabel(c.published_at)} · 首次收录 ${timeLabel(c.first_seen_at)}</p><p>${esc(c.codes.join(" / "))}</p><p>${c.sources.map((s) => eventLink({title:s.name,url:s.url})).join(" · ")}</p><p class="muted">${esc(c.basis)}</p><ul>${c.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>${c.reactions.map((r) => `<p class="muted">${esc(r.code)} · 收录后收盘基线 ${dateLabel(r.anchor_day)} → 后续${r.horizon}交易日 ${pct(r.return)} ${r.end_day ? dateLabel(r.end_day) : "等待完整观察"}</p>`).join("")}</article>`;
  $("evidence-cards").innerHTML = items.slice(0,8).map(card).join("") + (items.length > 8 ? `<details class="research-card"><summary>其余 ${items.length - 8} 条证据</summary>${items.slice(8).map(card).join("")}</details>` : "");
  $("evidence-count").textContent = `当前筛选${items.length}条 / 存档展示${data?.items?.length || 0}条 / 相关证据${data?.count || 0}条。${data?.method || "暂无已留存证据"}`;
}
function renderAISummary(data) {
  if (!data) { $("ai-summary").textContent = aiSummaryPending ? "AI摘要生成中，请稍候；正在核对输出格式与证据引用。" : "可直接核对下方证据。AI摘要需配置后手动生成，仅转述公开证据并提供引用。"; return; }
  const entries = (items) => items.map((i) => `<li>${esc(i.text)} ${i.evidence_ids.map((id) => `<a href="#evidence-${esc(id)}">[证据]</a>`).join(" ")}</li>`).join("");
  $("ai-summary").innerHTML = `<p class="muted">${esc(data.method)} · ${timeLabel(data.generated_at)}</p><h3>原文事实转述</h3><ul>${entries(data.facts)}</ul><h3>待核实问题</h3><ul>${entries(data.questions)}</ul>`;
}
function renderScorecard() {
  const cohort = $("tracking-cohort").value, horizon = Number($("tracking-horizon").value);
  $("scorecard-table").innerHTML = (trackingData?.groups || []).filter((g) => g.cohort === cohort).map((g) => {
    const s = g.summary.find((s) => s.horizon === horizon);
    return `<tr><td>${esc(g.label)}<small>${esc(g.rule_version)}${s.small_sample ? " · 样本不足" : ""}</small></td><td>${s.count} / ${s.pending} / ${s.missing}</td><td>${pct(s.mean_return)}<small>中位 ${pct(s.median_return)}</small></td><td>${pct(s.mean_excess)}<small>可比基准 ${s.benchmark_count} 条</small></td><td>${pct(s.p25)} / ${pct(s.p75)}</td><td>${num(s.invalidation_ratio,1)}%</td><td>${Object.entries(s.distribution).map(([k,v]) => `<small>${esc(k)}：${v}</small>`).join("")}</td></tr>`;
  }).join("") || emptyRow("当前范围没有已存信号",7);
}
let backtestId = 0;
async function loadBacktest() {
  const id = ++backtestId, day = state?.report?.day;
  if (!day) return;
  const form = $("backtest-form"), button = form.querySelector("button"), params = new URLSearchParams();
  for (const [k,v] of new FormData(form)) if (String(v).trim()) params.set(k,v);
  params.set("day",day);
  if (!selectedDay && !OFFLINE) params.set("live","true");
  button.disabled = true;
  try {
    const data = await api(`/api/backtest?${params}`);
    if (id !== backtestId || day !== state?.report?.day) return;
    if (!data) { $("backtest-output").textContent = "此离线文件尚未保存模拟结果，请重导出。"; return; }
    renderBacktest(data);
  } catch (error) { if (id === backtestId) $("backtest-output").textContent = error.message; }
  finally { if (id === backtestId) button.disabled = !!OFFLINE; }
}
function renderBacktest(data) {
  const valuation = data.valuation ? {...data.valuation} : null;
  if (OFFLINE && valuation?.status === "available") {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("sv-SE", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).formatToParts(new Date()).map((p) => [p.type,p.value]));
    const today = `${parts.year}${parts.month}${parts.day}`, clock = `${parts.hour}:${parts.minute}`;
    const cutoff = clock > "15:00" ? "15:00" : clock > "11:30" && clock < "13:00" ? "11:30" : clock;
    const expectedAt = new Date(`${parts.year}-${parts.month}-${parts.day}T${cutoff}:00+08:00`).getTime();
    if (today !== valuation.day || expectedAt - new Date(valuation.quote_start).getTime() > (valuation.interval_minutes + 5) * 60000) {
      valuation.status = "stale";
      valuation.message = "行情已过期，显示最后保存的盘中估值。" + valuation.message;
    }
  }
  const live = valuation?.summary;
  const s = live || data.summary, curve = [...(data.curve || [])];
  const positions = live ? valuation.positions : data.positions || [];
  if (live) curve.push({day:valuation.day,equity:s.equity,cash:s.available_cash,return:s.return,benchmark_return:s.benchmark_return});
  const valuationNotice = valuation ? `<div class="notice${valuation.status === "stale" || valuation.status === "unavailable" ? " error" : ""}"><b>${live ? "盘中持仓估值" : "日线账户估值"}</b><span>${esc(valuation.message)}${live ? ` 行情 ${timeLabel(valuation.quote_start)}～${timeLabel(valuation.quote_end)} · 采集 ${timeLabel(valuation.captured_at)}` : ""} · 交易时段每${valuation.interval_minutes}分钟采集${OFFLINE ? " · 公开页面需等待网站部署更新" : ""}</span></div>` : "";
  const p = data.parameters || {};
  const last = curve[curve.length - 1];
  const initial = s.initial_capital ?? p.capital ?? null;
  const cash = s.available_cash ?? last?.cash ?? null;
  const equity = s.equity ?? last?.equity ?? null;
  const marketValue = s.market_value ?? (equity !== null && cash !== null ? equity - cash : null);
  const profit = s.profit ?? (equity !== null && initial !== null ? equity - initial : null);
  const accountCards = `<div id="simulation-account" class="tracking-grid funds-grid">${[
    ["initial_capital","初始资金",initial,"本次模拟的起始资金"],
    ["market_value","当前持仓市值",marketValue,`按最近有效估值计算 · ${s.open_positions || 0}只持仓`],
    ["available_cash","可用资金",cash,"扣除已发生费用后的现金余额"],
    ["profit","盈亏金额",profit,"总资产减初始资金，已计入发生的费用"],
  ].map(([field,label,value,note]) => `<div class="tracking-card" data-account-field="${field}"><h3>${label}</h3><div class="metric-value ${field === "profit" ? color(value) : ""}">${field === "profit" && value > 0 ? "+" : ""}${num(value)}<small> 元</small></div><p>${esc(note)}</p></div>`).join("")}</div><p class="muted">总资产 ${num(equity)} 元 = 持仓市值 + 可用资金 · 账户估值截至 ${dateLabel(s.through || last?.day)}</p>`;
  const parameterText = `资金 ${num(p.capital,0)}元 · 单只权重 ${num(p.weight * 100,0)}% · 最多${p.max_positions}只 · 持有${p.holding_days}交易日 · 每边佣金${num(p.commission_bps,1)}基点（最低${num(p.min_commission)}元） · 卖出税费${num(p.sell_tax_bps,1)}基点 · 每边过户费${num(p.transfer_bps,1)}基点 · 每边滑点${num(p.slippage_bps,1)}基点 · 前一日量能参与上限${num(p.participation * 100,1)}%`;
  let chart = "";
  if (curve.length > 1) {
    const values = curve.flatMap((r) => [r.return,r.benchmark_return ?? 0]), low = Math.min(...values), span = Math.max(...values) - low || 1;
    const points = (key) => curve.map((r,i) => `${num(20 + i/(curve.length-1)*560)},${num(140 - ((r[key] ?? 0)-low)/span*110)}`).join(" ");
    chart = `<svg class="price-chart" viewBox="0 0 600 165" role="img" aria-label="模拟组合与沪深300累计变化"><polyline class="chart-line" points="${points("return")}"/><polyline fill="none" stroke="#b18c54" stroke-width="2" points="${points("benchmark_return")}"/></svg><p class="muted">绿色：模拟组合 · 棕色：沪深300 · ${dateLabel(curve[0].day)} → ${dateLabel(curve[curve.length-1].day)}${live ? " · 最后一点为所标时点的盘中估值" : ""}</p>`;
  }
  $("backtest-output").innerHTML = `${valuationNotice}${data.stop ? `<div class="notice error">${esc(data.stop.reason)} · ${dateLabel(data.stop.day)}；净值仅计算至 ${dateLabel(s.through)}</div>` : ""}${accountCards}<div class="tracking-grid"><div class="tracking-card"><h3>模拟组合累计变化</h3><div class="metric-value">${pct(s.return)}</div><p>沪深300同期 ${pct(s.benchmark_return)}</p></div><div class="tracking-card"><h3>日线组合最大回撤</h3><div class="metric-value">${pct(s.max_drawdown)}</div><p>闭合交易 ${s.closed_trades || 0} / 未平仓 ${s.open_positions || 0}</p></div><div class="tracking-card"><h3>累计模拟费用</h3><div class="metric-value">${num(s.fees)} 元</div><p>未平仓费用只含已发生的入场费用</p></div></div>${chart}<p class="muted">${esc(data.method)}</p><p class="muted">参数：${esc(parameterText)}${OFFLINE ? " · 离线快照固定参数，需在本地服务中修改" : ""}</p><div class="research-grid">${Object.entries(data.skipped || {}).map(([reason,count]) => `<div class="research-card">${esc(reason)}：${count}</div>`).join("")}</div>${(data.periods || []).map((p) => `<p>${esc(p.label)} ${dateLabel(p.from)} → ${dateLabel(p.to)}：${pct(p.return)}</p>`).join("")}<details class="panel research-panel"><summary>模拟成交明细 · ${(data.trades || []).length}笔</summary><div class="table-wrap"><table><thead><tr><th>股票</th><th>入场 / 退出</th><th>股数 / 费用</th><th>扣费变化</th><th>退出原因</th></tr></thead><tbody>${(data.trades || []).map((t) => `<tr><td>${esc(t.name)}<small>${esc(t.code)} · ${esc(t.strategy)}</small></td><td>${dateLabel(t.entry_day)} / ${dateLabel(t.exit_day)}<small>${num(t.entry_price)} / ${num(t.exit_price)}</small></td><td>${t.shares} / ${num(t.fees)}元</td><td>${pct(t.net_return)}</td><td>${esc(t.exit_reason)}</td></tr>`).join("") || emptyRow("暂无已完成交易；查看上方跳过原因或继续积累行情",5)}</tbody></table></div></details><details class="panel research-panel"><summary>当前模拟持仓 · ${positions.length}只</summary>${positions.map((p) => `<p class="research-body">${esc(p.name)} ${esc(p.code)} · ${p.shares}股 · 入场 ${dateLabel(p.entry_day)} · 最近估值 ${num(p.mark)}元${p.quote_time ? ` · 行情 ${timeLabel(p.quote_time)}` : ""}${p.exit_due ? ` · ${esc(p.exit_due)}` : ""}</p>`).join("")}</details>`;
}
function openPlan(code) {
  const watch = state.watchlist.find((w) => w.code === code), plan = watch?.plan || {}, form = $("plan-form");
  form.reset();
  $("plan-code").textContent = code;
  for (const name of ["reason","question","target","invalidation"]) form.elements[name].value = plan[name] ?? "";
  form.querySelectorAll('[name="checks"]').forEach((input) => input.checked = (plan.checks || ["below_ma20","down5","volume_down"]).includes(input.value));
  $("plan-error").textContent = "";
  $("plan-dialog").showModal();
}
$("evidence-search").addEventListener("input",renderEvidence);
$("tracking-cohort").addEventListener("change",renderScorecard);
$("tracking-horizon").addEventListener("change",renderScorecard);
$("backtest-form").addEventListener("submit",(e) => {e.preventDefault();loadBacktest();});
if (OFFLINE) $("backtest-form").querySelectorAll("input,select,button").forEach((input) => input.disabled = true);
$("plan-form")?.addEventListener("submit",async (e) => {
  e.preventDefault(); const form = e.target, fields = new FormData(form), button = form.querySelector("button");button.disabled = true;
  const payload = {reason:fields.get("reason"),question:fields.get("question"),target:fields.get("target") ? Number(fields.get("target")) : null,invalidation:fields.get("invalidation") ? Number(fields.get("invalidation")) : null,checks:fields.getAll("checks")};
  try {await api(`/api/watchlist/${$("plan-code").textContent}/plan`,{method:"PUT",headers:{"Content-Type":"application/json",Authorization:`Bearer ${fields.get("key")}`},body:JSON.stringify(payload)});form.reset();$("plan-dialog").close();await load();}
  catch (error) {$("plan-error").textContent = error.message;}finally {button.disabled = false;}
});
$("ai-summary-button")?.addEventListener("click",async () => {
  if (aiSummaryPending) return;
  const key = window.prompt("手动将本报告最多20条公开证据发送至已配置的AI接口生成摘要。请输入APP_API_KEY：");if(!key)return;
  const button = $("ai-summary-button"), day = state.report.day, label = button.textContent;
  aiSummaryPending = true;button.disabled = true;button.textContent = "生成中…";renderAISummary(null);
  try {const digest = await api(`/api/evidence/${day}/summary`,{method:"POST",headers:{Authorization:`Bearer ${key}`}});if(day === state.report.day)renderAISummary(digest);}
  catch (error) {if (day === state.report?.day) $("ai-summary").textContent = error.message;}
  finally {aiSummaryPending = false;button.textContent = label;button.disabled = !state.ai_configured;}
});
function renderCandidates() {
  if (!state?.report) return;
  const search = $("candidate-search").value.trim().toLowerCase();
  const all = state.report.all_candidates || state.report.candidates;
  const candidates = ($("candidate-scope").value === "all" || search ? all : state.report.candidates).filter((c) => (strategy === "all" || c.strategy === strategy) && `${c.name} ${c.code} ${c.industry}`.toLowerCase().includes(search));
  $("candidate-count").textContent = `${candidates.length} 展示 / ${all.length} 已存 / ${state.report.all_candidate_count} 命中`;
  $("candidate-table").innerHTML = candidates.length ? candidates.map((c) => `<tr><td><b>${esc(c.name)}</b><small>${esc(c.code)}</small>${c.reconstructed ? "<small>冻结指标重建</small>" : ""}</td><td><span class="signal-tag ${esc(c.strategy)}">${esc(c.label)}</span></td><td>${num(c.close)}<small class="${color(c.pct)}">${pct(c.pct)}</small></td><td class="${color(c.relative20)}">${c.relative20 > 0 ? "+" : ""}${num(c.relative20)} pp</td><td>${num(c.volume_ratio)} ×</td><td class="muted">${esc(c.industry)}</td><td><button class="row-detail" data-code="${esc(c.code)}" data-rule="${esc(c.strategy)}">查看依据 ↗</button></td></tr>`).join("") : emptyRow(state.report.candidates.length ? "没有符合当前搜索条件的信号" : "今天没有满足规则的观察信号；保持筛选标准，等待新的变化。",7);
}
function renderWatch(items) {
  $("watch-table").innerHTML = items.length ? items.map((w) => `<tr><td><b>${esc(w.name || w.code)}</b><small>${esc(w.code)}</small><small>${esc(w.note)}</small><small>${esc(w.plan?.reason || "")}</small><small>${esc(w.plan?.question || "")}</small><small>观察价 ${num(w.plan?.target)} / 失效价 ${num(w.plan?.invalidation)}</small></td><td>${num(w.close)}<small class="${color(w.pct)}">${pct(w.pct)}</small></td><td>${w.available ? esc(w.trend) : "历史不足"}</td><td><ul class="alert-list">${(w.changes || []).map((c) => `<li><b>${c.state === "triggered" ? "新触发" : "已解除"}：${esc(c.label)}</b></li>`).join("")}${w.evaluation?.notice ? `<li>${esc(w.evaluation.notice)}</li>` : '<li class="quiet">未出现新的计划条件变化</li>'}${w.evaluation?.baseline_day ? `<li class="quiet">比较基线 ${esc(w.evaluation.baseline_day)}</li>` : ""}${(w.alerts || []).map((a) => `<li class="quiet">当前检查：${esc(a)}</li>`).join("")}</ul></td><td>${OFFLINE ? "" : `<button class="edit-plan row-detail" data-code="${esc(w.code)}">观察计划</button><button class="remove-watch" data-code="${esc(w.code)}">移除</button>`}</td></tr>`).join("") : emptyRow("添加自选股，持续观察你关注的变化。",5);
}
async function showDetail(code, rule) {
  const candidate = (state.report.all_candidates || state.report.candidates).find((c) => c.code === code && c.strategy === rule);
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
    const day = state?.report?.day;
    const data = await api(`/api/tracking${day ? `?day=${day}` : ""}`);
    if (day !== state?.report?.day) return;
    trackingData = data;
    $("tracking-method").textContent = data.method;
    $("tracking-cards").innerHTML = data.summary.map((s) => `<div class="tracking-card"><h3>${s.horizon}个交易日 · 平均收盘变化</h3><div class="metric-value ${color(s.mean_return)}">${pct(s.mean_return)}</div><p>${s.count} 个完整观察 · 正变化占比 ${num(s.positive_ratio,1)}%</p><p>中位数 ${pct(s.median_return)} · 相对沪深300 ${pct(s.mean_excess)}</p><p>待观察 ${s.pending ?? 0} / 缺失 ${s.missing ?? 0}${s.small_sample ? " · 样本不足" : ""}</p><p>平均期间最大不利变化 ${pct(s.mean_adverse)}</p></div>`).join("");
    renderScorecard();
    const horizon = (item,n) => { const h = item.horizons[String(n)]; return h?.status === "ready" ? `<span class="${color(h.return)}">${pct(h.return)}</span>` : `<span class="muted">${h?.status === "missing" ? "数据缺失" : "观察中"}</span>`; };
    $("tracking-table").innerHTML = data.items.length ? data.items.map((item) => `<tr><td><b>${esc(item.name)}</b><small>${dateLabel(item.day)} · ${esc(item.code)}</small></td><td><span class="signal-tag ${esc(item.strategy)}">${esc(item.label)}</span></td><td>${horizon(item,5)}</td><td>${horizon(item,10)}</td><td>${horizon(item,20)}</td><td class="muted">${item.invalidated_on ? dateLabel(item.invalidated_on) : "尚无收盘失效记录"}</td></tr>`).join("") : emptyRow("信号发布后，系统会逐日积累观察记录。",6);
  } catch (error) { message(error.message,true); }
}
function switchView(view) {
  currentView = view;
  const text = {overview:["每日复盘","把今天的市场，看清楚。","市场广度、行业强弱与值得跟踪的变化。"],watch:["我的自选","持续关注，你在意的变化。","趋势检查与公告提醒，保留每一次观察的依据。"],tracking:["信号跟踪","让判断，经得起时间检验。","记录信号发布后的变化，逐步积累真实的观察样本。"],backtest:["模拟持仓","查看持仓与资金变化。","盘中每10分钟更新已确认持仓估值，历史回测按日线核验。"]}[view];
  $("breadcrumb").textContent = text[0]; $("page-title").textContent = text[1]; $("subtitle").textContent = text[2];
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active",b.dataset.view===view));
  document.querySelectorAll(".view").forEach((s) => s.hidden = s.id!==`view-${view}`);
  if (view === "tracking") loadTracking();
  if (view === "backtest") loadBacktest();
}
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click",() => switchView(b.dataset.view)));
document.querySelectorAll("[data-strategy]").forEach((b) => b.addEventListener("click",() => {strategy=b.dataset.strategy;document.querySelectorAll("[data-strategy]").forEach((s) => s.classList.toggle("active",s===b));renderCandidates();}));
$("candidate-search").addEventListener("input",renderCandidates);
$("candidate-scope").addEventListener("change",renderCandidates);
$("candidate-table").addEventListener("click",(e) => {const b=e.target.closest(".row-detail");if(b)showDetail(b.dataset.code,b.dataset.rule);});
$("date-select").addEventListener("change",() => {selectedDay = $("date-select").value; load();});
$("refresh").addEventListener("click",() => OFFLINE ? window.location.reload() : load());
$("method-button").addEventListener("click",() => $("method-dialog").showModal());
$("mobile-method").addEventListener("click",() => $("method-dialog").showModal());
$("add-watch").addEventListener("click",() => {$("watch-error").textContent="";$("watch-dialog").showModal();});
document.querySelectorAll(".close-dialog").forEach((b) => b.addEventListener("click",() => b.closest("dialog").close()));
document.querySelectorAll("dialog").forEach((d) => d.addEventListener("click",(e) => {if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}}));
$("watch-form")?.addEventListener("submit",async (e) => {
  e.preventDefault(); const form=e.target, button=form.querySelector("button[type=submit]"), fields=new FormData(form);button.disabled=true;
  try {await api("/api/watchlist",{method:"POST",headers:{"Content-Type":"application/json","Authorization":`Bearer ${fields.get("key")}`},body:JSON.stringify({code:String(fields.get("code")).trim().toUpperCase(),note:fields.get("note")})});form.reset();$("watch-dialog").close();await load();}
  catch(error){$("watch-error").textContent=error.message;}finally{button.disabled=false;}
});
$("watch-table").addEventListener("click",async (e) => {
  const edit = e.target.closest(".edit-plan");
  if (edit) { openPlan(edit.dataset.code); return; }
  const b=e.target.closest(".remove-watch");if(!b)return;const key=window.prompt(`移除 ${b.dataset.code}：请输入 APP_API_KEY`);if(!key)return;try{await api(`/api/watchlist/${b.dataset.code}`,{method:"DELETE",headers:{Authorization:`Bearer ${key}`}});await load();}catch(error){message(error.message,true);}
});
load().then(() => { if (OFFLINE?.portfolio) switchView("backtest"); });
setInterval(() => {
  if (!OFFLINE && state && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled && !$("backtest-form").querySelector("button").disabled) load();
}, 30000);

if (OFFLINE?.portfolio && OFFLINE.public && /^https?:$/.test(location.protocol)) {
  setInterval(() => { if (!document.hidden) location.replace(new URL("../portfolio.html", location.href)); }, 600000);
}
