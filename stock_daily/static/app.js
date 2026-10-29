"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (value, digits = 2) => value === null || value === undefined ? "—" : Number(value).toFixed(digits);
const pct = (value) => value === null || value === undefined ? "—" : `${value > 0 ? "+" : ""}${num(value)}%`;
const color = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "muted";
const dateLabel = (day) => day ? `${day.slice(0,4)}.${day.slice(4,6)}.${day.slice(6,8)}` : "—";
const timeLabel = (value) => !value ? "—" : Number.isNaN(new Date(value).getTime()) ? "时间待核对" : new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value));
const clockLabel = (value) => !value ? "—" : new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value));
const modelPeriod = (t,prefix) => t[`${prefix}_basis`] === "quote" ? "盘中报价模拟" : "开盘模拟";
const modelTime = (t,prefix) => t[`${prefix}_time`] ? clockLabel(t[`${prefix}_time`]) : dateLabel(t[`${prefix}_day`]);
const executedPlan = (i) => i.execution ? `<p class="trade-plan"><b>已模拟${i.execution.side === "buy" ? "买入" : "卖出"}</b> ${num(i.execution.shares,0)} 股 · ${num(i.execution.price,4)} 元<br>报价 ${clockLabel(i.execution.execution_time)} · 确认 ${clockLabel(i.execution.confirmed_at)}（北京时间）</p>` : "";
const emptyRow = (text, columns) => `<tr><td colspan="${columns}" class="empty-row">${esc(text)}</td></tr>`;
let state = null, strategy = "all", currentView = "overview", refreshId = 0, selectedDay = "", trackingData = null, aiSummaryPending = false;
const offlineElement = document.getElementById("offline-snapshot");
const OFFLINE = offlineElement ? JSON.parse(offlineElement.textContent) : null;
let publicSyncState = {status:"idle",text:"等待同步最新持仓"}, lastPublicApiAttempt = 0;

if (OFFLINE) {
  $("add-watch").hidden = true;
  $("date-select").disabled = true;
  $("refresh").textContent = OFFLINE.live_url && /^https?:$/.test(location.protocol) ? "↻ 更新持仓" : "↻ 重载文件";
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
  $("refresh").setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/dashboard${day ? `?day=${encodeURIComponent(day)}` : ""}`);
    if (id !== refreshId) return;
    if (state?.report?.day !== data.report?.day) $("research-output").textContent = "日期已更新，请重新运行研究。";
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
  finally { if (id === refreshId) { $("refresh").disabled = false; $("refresh").removeAttribute("aria-busy"); } }
}

function renderReport(data) {
  const report = data.report, m = report.market, q = report.quality;
  renderTradePlan(report.trade_plan);
  $("report-ai-content").innerHTML = analysisMarkup(data.report_analysis);
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
    return `<div class="sector-row"><span>${esc(s.name)}<small>${s.count}</small></span><svg viewBox="0 0 290 18" role="img" aria-label="${esc(s.name)}相对强度${num(s.relative20)}个百分点"><rect x="0" y="5" width="290" height="8" rx="3" fill="var(--surface-soft)"/><line x1="145" y1="2" x2="145" y2="16" stroke="var(--line)"/><rect x="${num(x)}" y="5" width="${num(width)}" height="8" rx="2" fill="${s.relative20 >= 0 ? 'var(--green)' : 'var(--blue)'}"/></svg><span class="sector-value ${color(s.relative20)}">${s.relative20 > 0 ? "+" : ""}${num(s.relative20)}</span></div>`;
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
  const kinds = {daily:"日线分析", announcements:"公告补查", midday:"午间快照", close:"收盘快照", evening:"晚间分析", email_queue:"邮件排队", email:"邮件发送"};
  $("run-list").innerHTML = data.runs.slice(0,4).map((r) => `<div class="list-entry"><div class="run-title"><span class="run-state ${esc(r.status)}">${labels[r.status] || esc(r.status)}</span><span>${esc(kinds[r.kind] || r.kind)}</span><span class="muted">${timeLabel(r.started_at)}</span></div><p>${esc(r.message)}</p></div>`).join("") || '<div class="list-entry muted">暂无任务记录</div>';
}
function eventLink(event) {
  let url;
  try { url = new URL(event.url); } catch (_) { return esc(event.title); }
  return ["https:","http:"].includes(url.protocol) ? `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(event.title)} ↗</a>` : esc(event.title);
}
function renderOperations(data) {
  if (!$("operations-content")) return;
  if ($("wecom-warning")) {
    const wecom = data?.wecom, counts = wecom?.counts || {};
    const pending = (counts.pending || 0) + (counts.sending || 0);
    const problem = {conflict:"机器人连接被其他服务接管，推送已暂停；请停用其他服务的同一机器人接入后恢复本项目连接",stale:"机器人连接心跳过期",stopped:"机器人连接已停止",reconnecting:"机器人正在重新连接"}[wecom?.connection?.status];
    const warning = wecom?.enabled && wecom.bound && (problem || counts.failed);
    $("wecom-warning").hidden = !warning;
    $("wecom-warning").textContent = warning ? `企业微信成交提醒异常：${problem || "存在发送失败的提醒"}。待发 ${pending} 笔，失败 ${counts.failed || 0} 笔；成交记录已保留，行情刷新不会重新交易。` : "";
  }
  if (!data) { $("operations-panel").hidden = true; return; }
  const labels = {running:"运行中", stopped:"已停止", stale:"心跳已过期", unknown:"尚未记录心跳"};
  $("worker-chip").textContent = labels[data.worker.status] || data.worker.status;
  $("worker-chip").className = `chip${data.worker.status === "running" ? "" : " amber"}`;
  const stages = {success:"完成", failed:"失败", running:"运行中", skipped:"跳过", not_run:"尚无记录"};
  $("operations-content").innerHTML = `<p>状态检查 ${timeLabel(data.checked_at)} · worker心跳 ${timeLabel(data.worker.at)} · ${esc(data.worker.phase || "尚无记录；请启动或重启worker")}${OFFLINE ? " · 此处为导出时的状态" : ""}</p><p>最新日线 ${dateLabel(data.latest_daily)} · 最新快照 ${dateLabel(data.latest_snapshot?.day)} ${esc(data.latest_snapshot?.slot || "")}</p><div class="research-grid">${data.next_tasks.map((t) => `<div class="research-card"><b>${esc({midday:"午间",close:"收盘",evening:"晚间"}[t.kind])} ${timeLabel(t.at)}</b><p>${t.calendar_verified ? "已保存交易日历中的下一时段" : "下一次时段检查，交易日历待核验"}</p></div>`).join("")}</div><div class="research-grid">${data.stages.map((s) => `<div class="research-card"><b>${esc(s.label)} · ${esc(stages[s.status] || s.status)}</b><p>${timeLabel(s.started_at)} · ${esc(s.message || "")}</p></div>`).join("")}</div><p>数据库备份：${data.backup_enabled ? esc({success:"成功",failed:"失败"}[data.backup.status] || "等待首次备份") : "未启用"} · ${timeLabel(data.backup.at)} ${esc(data.backup.file || "")}</p><p>盘中持仓估值：每${data.portfolio?.interval_minutes || 10}分钟 · ${esc({success:"已采集",failed:"采集失败",skipped:"非交易日",running:"采集中"}[data.portfolio?.status] || "等待交易时段")} · ${timeLabel(data.portfolio?.at)}</p><p>企业微信成交提醒：${data.wecom?.enabled ? (data.wecom.bound ? "已绑定" : "等待绑定") : "未配置或凭据不完整"} · ${esc({authenticated:"已连接",reconnecting:"重连中",conflict:"连接被接管",stale:"心跳过期"}[data.wecom?.connection?.status] || "等待连接")} · 已发送 ${data.wecom?.counts?.sent || 0} / 待发 ${(data.wecom?.counts?.pending || 0) + (data.wecom?.counts?.sending || 0)} / 失败 ${data.wecom?.counts?.failed || 0}</p><p>公开持仓轻量同步：${esc({success:"已同步",failed:"同步失败"}[data.portfolio_publish?.status] || "等待同步")} · 行情 ${timeLabel(data.portfolio_publish?.quote_at)} · 同步 ${timeLabel(data.portfolio_publish?.at)}</p>${data.pages.status ? `<p>公开网站：${esc(data.pages.status)} · ${timeLabel(data.pages.at)}；已触发上传不等于部署完成。</p>` : ""}`;
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
function analysisMarkup(data, title = "AI市场研判", prefix = "report-ai") {
  const labels = {available:"分析已完成",failed:"分析未通过",disabled:"未启用",unconfigured:"待配置",not_generated:"待生成"};
  const sections = {overview:"市场研判",focus:"关注方向",risks:"主要风险",watchpoints:"后续验证"};
  const cards = data?.status === "available" ? Object.entries(sections).map(([key,label]) => `<article class="research-card"><h3>${label}</h3>${(data.sections?.[key] || []).map(i=>`<p>${esc(i.text)}${GuanshiEvidence.references(i.evidence_ids || [],data.evidence || [],prefix)}</p>`).join("")}</article>`).join("") : "";
  const evidence = GuanshiEvidence.render(data?.evidence || [],prefix);
  return `<section class="panel research-panel ai-report-panel"><div class="panel-heading"><div><h2>${esc(title)}</h2><p>${esc(data?.message || "尚无该报告的AI研判；后台生成后随报告发布。")}</p></div><span class="chip">${labels[data?.status] || "等待分析"}</span></div><div class="research-body">${data?.as_of ? `<p class="muted">依据截止 ${clockLabel(data.as_of)} · 生成 ${clockLabel(data.generated_at)} · ${esc(data.model || "")}</p>` : ""}<div class="research-grid ai-sections">${cards}</div>${evidence ? `<details class="analysis-sources" data-evidence-key="${esc(`${prefix}:${data?.as_of || ""}:${data?.generated_at || ""}`)}"><summary>核对分析依据 · ${(data?.evidence || []).length} 项</summary>${evidence}</details>` : ""}</div></section>`;
}
function decisionMarkup(data) {
  if (!data?.enabled) return '<div class="notice"><span>当前成交仍沿用原量价规则；AI意见从启用后的下一批有效观察开始参与，不补造历史决策。</span></div>';
  return `<section class="panel research-panel"><div class="panel-heading"><div><h2>AI、舆情与模拟交易决策</h2><p>${esc(data.message)} · ${esc(data.version)}</p></div><span class="chip">${esc({normal:"正常风险",reduce:"风险减半",pause:"暂停新增"}[data.market_action] || "等待有效分析")}</span></div><div class="research-body"><p>${esc(data.method)}</p><p class="muted">意见截止 ${clockLabel(data.as_of)} · 生成 ${clockLabel(data.generated_at)}</p><details><summary>逐股意见 · ${Object.keys(data.opinions || {}).length} 只</summary><div class="research-grid">${Object.entries(data.opinions || {}).map(([code,i])=>`<article class="research-card"><h3>${esc(GuanshiEvidence.stockLabel(code,data.evidence || []))} · ${esc({support:"支持观察买入",wait:"等待",exit:"退出意见"}[i.action])}</h3><p>判断强度 ${num(i.score,0)} / 100（非胜率）</p><p>${esc(i.text)}</p>${GuanshiEvidence.references(i.evidence_ids || [],data.evidence || [],"decision-ai")}${(data.news_risks?.[code] || []).map(t=>`<p class="negative">待核对新闻：${esc(t)}</p>`).join("")}</article>`).join("")}</div></details>${data.evidence?.length ? `<details class="analysis-sources" data-evidence-key="${esc(`decision-ai:${data.as_of || ""}:${data.generated_at || ""}`)}"><summary>核对决策依据</summary>${GuanshiEvidence.render(data.evidence,"decision-ai")}</details>` : ""}</div></section>`;
}
function fillDecisionMarkup(data) {
  if (!data) return "";
  return `<details class="trade-evidence"><summary>AI与舆情决策依据</summary><p>${esc(data.reason)} · ${esc(data.version)}</p><p>${esc(data.market_reason || "")} · ${esc(data.market_action || "")}</p><p>观察 ${clockLabel(data.as_of)} · 生成 ${clockLabel(data.generated_at)} · ${esc(data.model || "")}</p><p>${esc(data.opinion?.action || "风控优先")} · 判断强度 ${num(data.score,0)}（非胜率）</p>${GuanshiEvidence.render(data.evidence || [])}</details>`;
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
function renderTradePlan(plan) {
  const output = $("trade-plan-content");
  if (!plan) { output.textContent = "此旧报告未保存下一交易日计划，请重导出日线报告。"; return; }
  const counts = plan.counts || {}, p = plan.parameters, scenario = plan.scenario;
  const today = new Date().toLocaleDateString("sv-SE", {timeZone:"Asia/Shanghai"}).replaceAll("-", "");
  const historical = plan.target_day && plan.target_day < today;
  const notice = plan.origin === "derived" ? "旧报告按截止日数据补算，非当时预先发布的计划。" : `计划保存 ${timeLabel(plan.created_at)}（北京时间）。`;
  const buy = (i) => `<article class="research-card plan-card"><h3>${esc(i.name)} <small>${esc(i.code)} · ${esc(i.label)} · 优先级 ${i.rank}</small></h3>
    <p><b>${esc({planned:"尝试买入",conditional:"先卖出后才尝试买入",watch:"仅观察 / 不买入",filled:"已模拟买入"}[i.status])}</b>：${esc(i.reason)}</p>
    ${executedPlan(i)}${entryObservation(i.intraday)}<div class="plan-facts"><p>收盘参考 <b>${num(i.reference_close,4)} 元</b><br>参考买入价 <b>${num(i.reference_price,4)} 元</b></p><p>参考股数 <b>${num(i.reference_shares,0)} 股</b><br>参考支出 <b>${num(i.reference_spent)} 元</b></p></div>
    <p>失效参考 ${num(i.invalidation_price,4)} 元${i.upper_open_exclusive ? ` · 开盘价须低于 ${num(i.upper_open_exclusive,4)} 元以避开接近涨停情形` : ""}</p>
    <details><summary>买入依据与执行条件</summary><p>信号发布 ${timeLabel(i.published_at)}（北京时间） · ${esc(i.rule_version || "版本未记录")}</p><ul>${(i.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}${(i.conditions || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul><ul>${(i.risks || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details></article>`;
  const sell = (i) => `<article class="research-card plan-card"><h3>${esc(i.name)} <small>${esc(i.code)} · ${num(i.shares,0)} 股</small></h3>${executedPlan(i)}<p><b>${i.execution ? "原卖出依据" : "下一有效报价尝试卖出"}</b>：${esc(i.reason)}</p><p>收盘参考 ${num(i.reference_close,4)} 元 · 参考卖出价 ${num(i.reference_price,4)} 元<br>参考净回款 ${num(i.reference_proceeds)} 元（扣卖出费用）</p><p>退出触发 ${dateLabel(i.exit_trigger_day)} · 已延期 ${(i.exit_delays || []).length} 个交易日</p><ul>${i.conditions.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></article>`;
  const hold = (i) => `<article class="research-card plan-card"><h3>${esc(i.name)} <small>${esc(i.code)} · ${num(i.shares,0)} 股</small></h3><p>${esc(i.reason)}</p><p>已核验持有 ${i.held_sessions} / ${p.holding_days} 个交易日<br>失效参考 ${num(i.invalidation_price,4)} 元 · 收盘参考 ${num(i.reference_close,4)} 元</p></article>`;
  const actionable = (plan.entries || []).filter((i) => i.status !== "watch"), watch = (plan.entries || []).filter((i) => i.status === "watch");
  output.innerHTML = `<div class="notice${plan.status === "blocked" || !plan.calendar_verified ? " error" : ""}"><span>依据 ${dateLabel(plan.basis_day)} 日线 → ${plan.target_day ? `计划交易日 ${dateLabel(plan.target_day)}` : "下一交易日待日历核验，不推测日期"}。${esc(notice)}${historical ? " 目标日已过，这是该报告日期的计划，不代表明天的新计划。" : ""}${plan.status === "late" ? " 计划生成晚于目标开盘，仅供回放核对。" : ""}</span></div>
    ${plan.status === "blocked" ? `<p class="negative">${esc(plan.message)}${plan.stop ? ` ${esc(plan.stop.reason)}` : ""}</p>` : `
    ${plan.execution ? `<p class="trade-plan">盘中执行截至 ${clockLabel(plan.execution.confirmed_at)}（北京时间） · 已模拟买入 ${plan.execution.buys} 笔 · 已模拟卖出 ${plan.execution.sells} 笔</p>` : ""}
    <p>待尝试买入 ${(plan.entries || []).filter(i => i.status === "planned").length} 只 · 依赖卖出 ${(plan.entries || []).filter(i => i.status === "conditional").length} 只 · 待卖出 ${(plan.exits || []).filter(i => !i.execution).length} 只 · 继续持有 ${counts.holds || 0} 只 · 仅观察 ${counts.watch || 0} 条</p>
    ${scenario ? `<p class="muted">原计划现金 ${num(scenario.cash_before_sales)} 元 · 按收盘参考价测算全部卖出时，参考回款 ${num(scenario.reference_sale_proceeds)} 元、参考现金 ${num(scenario.reference_cash_after_sales)} 元。${esc(scenario.note)}</p>` : ""}
    <h3>卖出计划与执行</h3><div class="research-grid">${(plan.exits || []).map(sell).join("") || '<p class="muted">暂无按当前规则需要退出的模拟持仓。</p>'}</div>
    ${plan.entry_policy ? `<p class="trade-plan">盘中买入重评 · ${clockLabel(plan.entry_policy.evaluated_at)}（北京时间）<br>${esc(plan.entry_policy.method)}</p>` : ""}
    <h3>待买入候选</h3><div class="research-grid">${actionable.map(buy).join("") || '<p class="muted">暂无满足参考资金与仓位条件的买入计划；查看下方仅观察原因。</p>'}</div>
    <details><summary>继续持有 · ${counts.holds || 0} 只</summary><div class="research-grid">${(plan.holds || []).map(hold).join("") || "暂无继续持有记录"}</div></details>
    <details><summary>仅观察 / 不买入 · ${watch.length} 条</summary><div class="research-grid">${watch.map(buy).join("") || "暂无"}</div></details>`}
    <details><summary>计划规则与默认参数</summary><p>${esc(plan.method)}</p><p>初始资金 ${num(p.capital,0)} 元 · 单只 ${num(p.weight * 100,0)}% · 最多 ${p.max_positions} 只 · 持有 ${p.holding_days} 交易日 · 每边滑点 ${p.slippage_bps} 基点 · 前一日量能参与上限 ${num(p.participation * 100,1)}%</p></details>`;
}
let tradeRows = [], tradeSearch = "", tradeStatus = "all", tradeCutoff = "", tradeHoldingDays = 10;
const entryObservation = (e) => {
  if (!e) return "";
  const status = {paused:"暂缓买入",waiting:"等待连续确认",eligible:"确认通过",cancelled:"当日已取消",filled:"已模拟买入"}[e.status];
  return `<p class="trade-plan" data-entry-status="${esc(e.status || "")}">盘中确认 ${esc(e.version)} · ${clockLabel(e.evaluated_at || e.quote_time)}${status ? `<br>${status}` : ""}<br>${esc(e.reason)}${e.origin === "intraday" ? `<br>来源：盘中新发现 · 首次发现 ${clockLabel(e.discovered_at)}` : ""}${e.vwap != null ? `<br>当日成交均价 ${num(e.vwap,4)} 元 · 当日相对沪深300 ${num(e.relative_day)} pp` : ""}${e.max_entry_gap != null ? `<br>本次追高上限 ${num(e.max_entry_gap * 100)}% · 回落容忍度 ${num(e.max_quote_pullback * 100)}%${e.confirmation_mode === "gap_recovery" ? " · 高开回落后重新确认" : ""}` : ""}${e.interval_volume != null ? `<br>观察区间 ${clockLabel(e.previous_quote_time)}～${clockLabel(e.quote_time)} · 新增成交 ${num(e.interval_volume,0)} 股 · 区间相对沪深300 ${num(e.relative_interval)} pp` : ""}</p>`;
};
const exitInitialRule = (p) => p.version === "exit-v3" ? (p.atr_missing ? "新仓初始保护：ATR缺失，按原失效位和6%线保护" : "新仓初始保护距离1～2ATR，6%保护线优先") : p.version === "exit-v2" ? (p.atr_missing ? "保留原初始保护：ATR缺失，按原失效位和6%线保护" : "保留原初始保护：原失效位、2ATR与6%线取较高者") : "";
const exitProtection = (t) => t.exit_policy ? `<p class="trade-plan" data-exit-version="${esc(t.exit_policy.version)}">退出保护 · ${esc(t.exit_policy.version)}<br>${exitInitialRule(t.exit_policy)}<br>初始保护价 ${num(t.exit_policy.initial_stop,4)} 元 · 当前保护价 <b>${num(t.exit_policy.stop,4)} 元</b> · ${t.exit_policy.trailing_active ? "移动盈利保护已启用" : `浮盈达到2R启用移动保护（1R=${num(t.exit_policy.risk_unit,4)}元/股）`}<br>启用后已观察高点 ${num(t.exit_policy.peak,4)} 元${t.exit_policy.ma20 ? ` · 最近完整日线MA20 ${num(t.exit_policy.ma20,4)} 元` : ""}</p>` : "";
const exitSlippageNote = (t) => t.exit_slippage_below_low ? `<p class="trade-plan" data-exit-slippage-below-low="true">卖出滑点后的模拟成交价低于已观察当日最低价 ${num(t.exit_observed_low,4)} 元；这是模拟滑点假设，不代表真实撮合。</p>` : "";
const tradeEvidenceOpen = new Set();
function rememberTradeEvidence() {
  document.querySelectorAll(".trade-evidence").forEach((element) => {
    if (element.open) tradeEvidenceOpen.add(element.dataset.tradeKey);
    else tradeEvidenceOpen.delete(element.dataset.tradeKey);
  });
}
function matchingTrades() {
  const query = tradeSearch.trim().toLowerCase();
  return tradeRows.filter((t) => (tradeStatus === "all" || t.status === tradeStatus) &&
    [t.name,t.code,t.label,t.strategy,t.rule_version].join(" ").toLowerCase().includes(query));
}
function tradeCard(t) {
  const closed = t.status === "closed", delays = t.exit_delays || [];
  const key = `${t.code}|${t.entry_day}|${t.strategy}`;
  const profit = t.profit ?? (closed ? t.shares * (t.exit_price - t.entry_price) - t.fees : t.shares * t.mark - t.spent);
  const status = closed ? "已卖出" : t.exit_due ? "待退出" : "持仓中";
  const reason = t.exit_reason || t.exit_due;
  return `<article class="trade-card" data-code="${esc(t.code)}" data-status="${esc(t.status)}">
    <header><div><h3>${esc(t.name)} <small>${esc(t.code)}</small></h3><p>${esc(t.label || t.strategy)} · ${esc(t.rule_version || "版本未记录")}</p></div><span class="chip${!closed && t.exit_due ? " amber" : ""}">${status}</span></header>
    <dl class="trade-prices"><div><dt>买入时间</dt><dd>${modelTime(t,"entry")}<small>${modelPeriod(t,"entry")}</small></dd></div><div><dt>买入价格</dt><dd>${num(t.entry_price,4)} 元<small>已计买入滑点</small></dd></div>
    <div><dt>卖出时间</dt><dd>${closed ? modelTime(t,"exit") : "未卖出"}<small>${closed ? modelPeriod(t,"exit") : "尚未确认模拟卖出"}</small></dd></div><div><dt>卖出价格</dt><dd>${closed ? `${num(t.exit_price,4)} 元` : "—"}<small>${closed ? "已计卖出滑点" : "不以估值代替卖出价"}</small></dd></div></dl>
    ${entryObservation(t.entry_confirmation)}${fillDecisionMarkup(t.exit_decision || t.entry_decision)}${exitProtection(t)}${t.quote_error ? `<p class="negative">${esc(t.quote_error)}；此股票沿用最近估值，暂停成交。</p>` : ""}${closed ? "" : `<p class="trade-mark">最近估值 <b>${num(t.mark,4)} 元</b> · ${t.quote_time ? timeLabel(t.quote_time) + "（北京时间）" : dateLabel(tradeCutoff) + " 日线"}</p>`}
    <div class="trade-result"><span>${closed ? "已实现盈亏" : "持仓浮盈亏"}<b class="${color(profit)}">${profit > 0 ? "+" : ""}${num(profit)} 元</b></span><span>扣费变化<b class="${color(t.net_return)}">${pct(t.net_return)}</b></span></div>
    <p class="muted">${num(t.shares,0)} 股 · ${t.held_sessions ?? "—"} 个已核验持有交易日 · ${closed ? "双边费用" : "已发生买入费用"} ${num(closed ? t.fees : t.entry_fee)} 元${closed ? "" : "，浮盈亏未扣未来卖出费用"}</p>
    <p class="trade-plan">${reason ? `${closed ? "退出原因" : "待退出原因"}：${esc(reason)}${t.exit_trigger_day ? `；${t.exit_trigger_kind === "quote" ? clockLabel(t.exit_trigger_time) + " 盘中触发" : dateLabel(t.exit_trigger_day) + " 收盘触发"}` : ""}` : `退出观察：${t.exit_policy ? "每次有效报价检查保护价；连续两日收盘低于MA20或" : `收盘跌破 ${num(t.invalidation_price,4)} 元，或`}持有期满 ${tradeHoldingDays} 个交易日触发退出，遵守T+1。`}</p>
    ${delays.length ? `<p class="negative">退出受限，已延期 ${delays.length} 个交易日；${closed ? "下方保留延期记录。" : "仍未模拟卖出。"}</p>` : ""}
    <details class="trade-evidence" data-trade-key="${esc(key)}"${tradeEvidenceOpen.has(key) ? " open" : ""}><summary>查看信号依据与成交计算</summary><p>信号日 ${dateLabel(t.signal_day)} · 发布 ${timeLabel(t.published_at)}（北京时间）</p>
    ${t.entry_reasons?.length ? `<ul>${t.entry_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "<p>原信号未保存具体买入依据，请核对该日原始报告。</p>"}
    <p>${t.entry_basis === "quote" ? "买入报价" : "买入开盘价"} ${num(t.entry_reference_price ?? t.entry_open,4)} 元 → 模拟买入价 ${num(t.entry_price,4)} 元；买入费用 ${num(t.entry_fee)} 元；买入支出 ${num(t.spent)} 元。</p>
    ${closed ? `<p>${t.exit_basis === "quote" ? "卖出报价" : "卖出开盘价"} ${num(t.exit_reference_price ?? t.exit_open,4)} 元 → 模拟卖出价 ${num(t.exit_price,4)} 元；卖出费用 ${num(t.exit_fee)} 元；卖出净收入 ${num(t.proceeds)} 元。</p>${exitSlippageNote(t)}` : ""}
    ${t.exit_trigger_kind === "quote" ? `<p>盘中退出触发 ${clockLabel(t.exit_trigger_time)} · 报价 ${num(t.exit_trigger_price,4)} 元；触发价不保证成交。</p>` : t.exit_trigger_day ? `<p>退出触发日 ${dateLabel(t.exit_trigger_day)} · 当日收盘 ${num(t.exit_trigger_close,4)} 元；该价是判断依据，不是保证成交价。</p>` : ""}
    <p>原信号失效价 ${num(t.invalidation_price,4)} 元；历史开盘回放保留日期；盘中模拟保存报价及确认时间。</p>
    ${t.entry_risk?.risk_budget ? `<p>入场风险预算 ${num(t.entry_risk.risk_budget)} 元 · 预计风险 ${num(t.entry_risk.estimated_risk)} 元（含预计双边费用） · 风险距离 ${num(t.entry_risk.risk_distance,4)} 元${t.entry_risk.risk_stop != null ? ` · 风险预算保护价 ${num(t.entry_risk.risk_stop,4)} 元` : ""}。跳空或退出延期可能超过预算。</p>` : ""}
    ${delays.length ? `<ul>${delays.map((d) => `<li>${dateLabel(d.day)}：${esc(d.reason)}</li>`).join("")}</ul>` : ""}</details></article>`;
}
function renderTradeLedger() {
  rememberTradeEvidence();
  const rows = matchingTrades();
  $("trade-count").textContent = `显示 ${rows.length} / ${tradeRows.length} 笔 · 持仓 ${tradeRows.filter((t) => t.status === "open").length} · 已卖出 ${tradeRows.filter((t) => t.status === "closed").length}`;
  $("trade-ledger").innerHTML = rows.map(tradeCard).join("") || '<p class="muted">暂无符合条件的模拟交易。最新信号于下一交易日按有效报价核验；未成交原因见下方。</p>';
  $("export-trades").disabled = !rows.length;
}
function exportTradeLedger() {
  const rows = matchingTrades();
  // Escape formula-like cells as text for spreadsheets, including untrusted names/reasons.
  const cell = (value) => {
    const text = typeof value === "number" ? String(value) : String(value ?? "").replace(/^([\s]*[=+@-])/, "'$1");
    return `"${text.replaceAll('"','""')}"`;
  };
  const header = ["股票代码","名称","状态","策略","版本","信号日期","发布时间（北京时间）","买入日期","买入时段","买入开盘价","模拟买入价","卖出日期","卖出时段","卖出开盘价","模拟卖出价","股数","买入费用","卖出费用","已发生费用","买入支出","卖出净收入","最近估值价","估值时间","盈亏金额","扣费变化(%)","已核验持有交易日","失效价","退出触发日期","触发收盘价","退出原因","退出延期记录","买入依据","买入报价时间","买入确认时间","买入参考价","卖出报价时间","卖出确认时间","卖出参考价","退出触发类型","盘中退出触发时间","盘中退出触发价","退出规则版本","当前保护价","盘中买入确认依据","AI与舆情决策依据","风险预算保护价","卖出滑点低于已观察低点","卖出已观察当日最低价"];
  const values = rows.map((t) => [t.code,t.name,t.status === "closed" ? "已卖出" : t.exit_due ? "待退出" : "持仓中",t.label || t.strategy,t.rule_version,t.signal_day,t.published_at,t.entry_day,modelPeriod(t,"entry"),t.entry_open,t.entry_price,t.exit_day,t.exit_day ? modelPeriod(t,"exit") : "",t.exit_open,t.exit_price,t.shares,t.entry_fee,t.exit_fee,t.status === "closed" ? t.fees : t.entry_fee,t.spent,t.proceeds,t.status === "open" ? t.mark : null,t.status === "open" ? t.quote_time || tradeCutoff : null,t.profit,t.net_return,t.held_sessions,t.invalidation_price,t.exit_trigger_day,t.exit_trigger_close,t.exit_reason || t.exit_due,(t.exit_delays || []).map((d) => `${d.day} ${d.reason}`).join("；"),(t.entry_reasons || []).join("；"),t.entry_time,t.entry_confirmed_at,t.entry_reference_price ?? t.entry_open,t.exit_time,t.exit_confirmed_at,t.exit_reference_price ?? t.exit_open,t.exit_trigger_kind,t.exit_trigger_time,t.exit_trigger_price,t.exit_policy?.version,t.exit_policy?.stop,t.entry_confirmation ? JSON.stringify(t.entry_confirmation) : "",JSON.stringify({entry:t.entry_decision,exit:t.exit_decision}),t.entry_risk?.risk_stop,t.exit_slippage_below_low ?? false,t.exit_observed_low]);
  const blob = new Blob(["\ufeff" + [header,...values].map((row) => row.map(cell).join(",")).join("\r\n")], {type:"text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url; link.download = `模拟交易明细-${tradeCutoff || "最新"}.csv`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
let fillRows = [], fillHistoryOpen = false;
const fillFilter = {query:"",side:"all",from:"",to:"",page:1,size:20};
function legacyFillRows(data) {
  const source = OFFLINE?.dashboard?.source || state?.source || "";
  const make = (t,side) => {
    const sell = side === "sell", prefix = sell ? "exit" : "entry";
    const price = t[`${prefix}_price`], fee = t[`${prefix}_fee`], opening = t[`${prefix}_open`];
    const reference = t[`${prefix}_reference_price`] ?? opening;
    const notional = price != null && t.shares != null ? price * t.shares : null;
    const roundTrip = `${source}|${t.code}|${t.strategy}|${t.entry_day}`;
    const flow = sell ? t.proceeds ?? (notional != null && fee != null ? notional - fee : null) : t.spent ?? (notional != null && fee != null ? notional + fee : null);
    return {id:`${roundTrip}|${side}`,round_trip_id:roundTrip,source,sequence:null,day:t[`${prefix}_day`],side,code:t.code,name:t.name,strategy:t.strategy,label:t.label,rule_version:t.rule_version,
      shares:t.shares,price,open_price:opening,reference_price:reference,execution_time:t[`${prefix}_time`],confirmed_at:t[`${prefix}_confirmed_at`],period:modelPeriod(t,prefix),notional,fee,cash_flow:flow == null ? null : sell ? flow : -flow,cash_after:null,
      slippage_cost:price != null && reference != null && t.shares != null ? t.shares * (sell ? reference - price : price - reference) : null,
      signal_day:t.signal_day,published_at:t.published_at,entry_day:t.entry_day,entry_price:t.entry_price,entry_fee:t.entry_fee,invalidation_price:t.invalidation_price,
      entry_reasons:t.entry_reasons || [],entry_risk:t.entry_risk,realized_profit:sell ? t.profit : null,
      reason:sell ? t.exit_reason : "已发布信号，下一交易日开盘模拟入场",exit_trigger_day:sell ? t.exit_trigger_day : null,
      exit_trigger_close:sell ? t.exit_trigger_close : null,exit_delays:sell ? t.exit_delays || [] : [],origin:"legacy_derived"};
  };
  return [...(data.trades || []).flatMap((t) => [make(t,"buy"),make(t,"sell")]),...(data.positions || []).map((t) => make(t,"buy"))]
    .sort((a,b) => String(b.day).localeCompare(String(a.day)) || b.id.localeCompare(a.id));
}
function matchingFills() {
  const query = fillFilter.query.trim().toLowerCase(), from = fillFilter.from.replaceAll("-",""), to = fillFilter.to.replaceAll("-","");
  return fillRows.filter((f) => (fillFilter.side === "all" || f.side === fillFilter.side) && (!from || f.day >= from) && (!to || f.day <= to) &&
    [f.code,f.name,f.label,f.strategy,f.rule_version,f.id].join(" ").toLowerCase().includes(query));
}
function renderFillHistory() {
  const rows = matchingFills(), pages = Math.max(1,Math.ceil(rows.length / fillFilter.size));
  fillFilter.page = Math.min(Math.max(1,fillFilter.page),pages);
  const start = (fillFilter.page - 1) * fillFilter.size, visible = rows.slice(start,start + fillFilter.size);
  const invalidRange = fillFilter.from && fillFilter.to && fillFilter.from > fillFilter.to;
  $("fill-count").textContent = invalidRange ? "起始日期不能晚于结束日期，请调整筛选条件。" : `共 ${rows.length} / ${fillRows.length} 笔成交 · 买入 ${rows.filter((f) => f.side === "buy").length} / 卖出 ${rows.filter((f) => f.side === "sell").length} · 本页 ${visible.length} 笔 · 历史截止 ${dateLabel(tradeCutoff)}`;
  $("fill-table").innerHTML = visible.map((f) => `<tr><td>${f.execution_time ? clockLabel(f.execution_time) : dateLabel(f.day)}<small>${esc(f.period || "开盘模拟")}</small></td><td>${esc(f.name)} <span class="fill-side-tag">${f.side === "buy" ? "买入" : "卖出"}</span><small>${esc(f.code)}</small></td><td>${f.side === "buy" ? "买入" : "卖出"}</td><td>${num(f.price,4)}</td><td>${num(f.shares,0)}</td><td>${num(f.notional)}</td><td>${num(f.fee)}</td><td>${num(f.cash_flow)}</td><td>${f.side === "sell" ? num(f.realized_profit) : "—"}</td><td><button type="button" class="row-detail" data-fill-id="${esc(f.id)}">查看详情</button></td></tr>`).join("") || emptyRow("暂无符合筛选条件的成交。未成交计划和盘中估值不计入历史成交。",10);
  $("fill-page").textContent = `第 ${fillFilter.page} / ${pages} 页`;
  $("fill-prev").disabled = fillFilter.page <= 1;
  $("fill-next").disabled = fillFilter.page >= pages;
  $("export-fills").disabled = !rows.length;
}
function openFillDetail(id) {
  const f = fillRows.find((item) => item.id === id);
  if (!f) return;
  const sell = f.side === "sell", risk = f.entry_risk;
  $("fill-detail-title").textContent = `${f.name || f.code} · ${sell ? "卖出" : "买入"}成交详情`;
  $("fill-detail-body").innerHTML = `<p>${f.execution_time ? clockLabel(f.execution_time) : dateLabel(f.day)} · 北京时间 · ${esc(f.period || "开盘模拟")}${f.confirmed_at ? ` · 确认 ${clockLabel(f.confirmed_at)}` : ""}</p><dl class="trade-prices">
    ${[["模拟成交价",`${num(f.price,4)} 元`],["股数",`${num(f.shares,0)} 股`],["成交金额",`${num(f.notional)} 元`],["本笔费用",`${num(f.fee)} 元`],["现金变化",`${num(f.cash_flow)} 元`],["成交后现金",`${num(f.cash_after)} 元`]].map(([k,v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>
    <p>${f.execution_time ? "成交参考报价" : "开盘原价"} ${num(f.reference_price ?? f.open_price,4)} 元 → 模拟成交价 ${num(f.price,4)} 元 · 本笔滑点成本 ${num(f.slippage_cost)} 元（已反映在成交价中）。</p>${sell ? exitSlippageNote(f) : ""}
    <p>${sell ? `该笔卖出结束的交易扣费已实现盈亏 ${num(f.realized_profit)} 元，包含买卖双边费用；原买入 ${dateLabel(f.entry_day)}，买入价 ${num(f.entry_price,4)} 元，买入费用 ${num(f.entry_fee)} 元。` : "买入成交本身不确认已实现盈亏；盈亏记录在对应卖出成交中，本笔费用仅为已发生买入费用。"}</p>
    <p>${esc(f.label || f.strategy)} · ${esc(f.rule_version || "版本未记录")} · 信号日 ${dateLabel(f.signal_day)} · 发布 ${timeLabel(f.published_at)}（北京时间）</p>
    <p>${esc(f.reason)}</p><ul>${(f.entry_reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    <p>失效参考 ${num(f.invalidation_price,4)} 元；不是保证成交价。</p>
    ${risk?.risk_budget ? `<p>入场风险预算 ${num(risk.risk_budget)} 元 · 预计风险 ${num(risk.estimated_risk)} 元 · 风险距离 ${num(risk.risk_distance,4)} 元${risk.risk_stop != null ? ` · 风险预算保护价 ${num(risk.risk_stop,4)} 元` : ""}。跳空或退出延期可能超过预算。</p>` : ""}
    ${entryObservation(f.entry_confirmation)}${fillDecisionMarkup(f.decision_context)}${exitProtection(f)}${sell && f.exit_trigger_kind === "quote" ? `<p>盘中触发 ${clockLabel(f.exit_trigger_time)} · 报价 ${num(f.exit_trigger_price,4)} 元。</p>` : sell && f.exit_trigger_day ? `<p>退出触发 ${dateLabel(f.exit_trigger_day)} · 当日收盘 ${num(f.exit_trigger_close,4)} 元。</p>` : ""}
    ${(f.exit_delays || []).length ? `<ul>${f.exit_delays.map((d) => `<li>${dateLabel(d.day)}：${esc(d.reason)}</li>`).join("")}</ul>` : ""}
    <p class="muted">成交编号 ${esc(f.id)}${f.sequence != null ? ` · 模拟执行序号 ${f.sequence}` : ""}</p>
    <p class="muted">${f.origin === "legacy_derived" ? "此旧版成交由买卖明细恢复，未保存的执行顺序及成交后现金留空。 " : ""}${f.execution_time ? "盘中模拟记录，保存当时的报价及确认时间。" : "日线模拟记录，未保存分钟或秒级成交时间。"}</p>`;
  $("fill-dialog").showModal();
}
function exportFillHistory() {
  const cell = (value) => `"${(typeof value === "number" ? String(value) : String(value ?? "").replace(/^([\s]*[=+@-])/, "'$1")).replaceAll('"','""')}"`;
  const header = ["成交编号","成交日期","时段","方向","代码","名称","策略","规则版本","股数","开盘原价","模拟成交价","成交金额","本笔费用","现金变化","成交后现金","本笔滑点成本","卖出对应交易已实现盈亏","信号日期","信号发布时间","原买入日期","原买入价","原买入费用","失效价","入场风险预算","预计入场风险","退出触发日","退出触发收盘价","成交原因","原始信号依据","退出延期记录","执行序号","记录来源","报价时间（北京时间）","确认时间（北京时间）","参考报价","退出触发类型","盘中退出触发时间","盘中退出触发价","退出规则版本","当前保护价","盘中买入确认依据","AI与舆情决策依据","风险预算保护价","卖出滑点低于已观察低点","卖出已观察当日最低价"];
  const values = matchingFills().map((f) => [f.id,f.day,f.period || "开盘模拟",f.side === "buy" ? "买入" : "卖出",f.code,f.name,f.label || f.strategy,f.rule_version,f.shares,f.open_price,f.price,f.notional,f.fee,f.cash_flow,f.cash_after,f.slippage_cost,f.realized_profit,f.signal_day,f.published_at,f.entry_day,f.entry_price,f.entry_fee,f.invalidation_price,f.entry_risk?.risk_budget,f.entry_risk?.estimated_risk,f.exit_trigger_day,f.exit_trigger_close,f.reason,(f.entry_reasons || []).join("；"),(f.exit_delays || []).map((d) => `${d.day} ${d.reason}`).join("；"),f.sequence,f.origin,f.execution_time,f.confirmed_at,f.reference_price ?? f.open_price,f.exit_trigger_kind,f.exit_trigger_time,f.exit_trigger_price,f.exit_policy?.version,f.exit_policy?.stop,f.entry_confirmation ? JSON.stringify(f.entry_confirmation) : "",f.decision_context ? JSON.stringify(f.decision_context) : "",f.entry_risk?.risk_stop,f.exit_slippage_below_low ?? false,f.exit_observed_low]);
  const url = URL.createObjectURL(new Blob(["\ufeff" + [header,...values].map((r) => r.map(cell).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
  const link = document.createElement("a");link.href = url;link.download = `历史全部成交-${tradeCutoff || "最新"}.csv`;
  link.click();setTimeout(() => URL.revokeObjectURL(url),1000);
}
function setupFillHistory() {
  for (const [id,key] of [["fill-search","query"],["fill-side","side"],["fill-from","from"],["fill-to","to"],["fill-size","size"]]) {
    $(id).value = fillFilter[key];
    $(id).addEventListener(id === "fill-search" ? "input" : "change", (event) => {
      fillFilter[key] = key === "size" ? Number(event.target.value) : event.target.value;
      fillFilter.page = 1;renderFillHistory();
    });
  }
  $("fill-prev").addEventListener("click", () => {fillFilter.page -= 1;renderFillHistory();});
  $("fill-next").addEventListener("click", () => {fillFilter.page += 1;renderFillHistory();});
  $("fill-reset").addEventListener("click", () => {
    Object.assign(fillFilter,{query:"",side:"all",from:"",to:"",page:1});
    for (const [id,key] of [["fill-search","query"],["fill-side","side"],["fill-from","from"],["fill-to","to"]]) $(id).value = fillFilter[key];
    renderFillHistory();
  });
  $("export-fills").addEventListener("click",exportFillHistory);
  $("fill-table").addEventListener("click", (event) => {const button = event.target.closest("[data-fill-id]");if (button) openFillDetail(button.dataset.fillId);});
  $("fill-history").addEventListener("toggle", () => {fillHistoryOpen = $("fill-history").open;});
  $("show-fill-history").addEventListener("click", () => {$("fill-history").open = true;fillHistoryOpen = true;$("fill-history").scrollIntoView({block:"start"});});
  renderFillHistory();
}
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
function researchPanels(data) {
  const a = data.attribution, study = data.forward_study;
  let markup = "";
  if (a) {
    const table = (rows) => `<div class="table-wrap"><table><thead><tr><th>分组</th><th>已平仓 / 持仓</th><th>已实现</th><th>浮动</th><th>合计盈亏</th><th>费用</th><th>平仓胜率</th><th>平均持有交易日</th><th>样本</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.label || r.key)}</td><td>${r.closed} / ${r.open}</td><td>${num(r.realized)}</td><td>${num(r.unrealized)}</td><td class="${color(r.profit)}">${num(r.profit)}</td><td>${num(r.fees)}</td><td>${pct(r.win_rate)}</td><td>${num(r.average_holding_sessions,1)}</td><td>${r.small_sample ? '不足30笔' : '仍需前向验证'}</td></tr>`).join('') || '<tr><td colspan="9">暂无成交</td></tr>'}</tbody></table></div>`;
    markup += `<section class="panel research-panel" id="account-attribution"><h2>策略归因 · 盈亏来自哪里</h2><p>账户截至 ${dateLabel(a.day)}${a.as_of ? ' · 行情 '+clockLabel(a.as_of) : ''} · 下方日线绩效截至 ${dateLabel(a.daily_through)}。金额单位：元。</p><p class="${a.reconciled ? 'muted' : 'error'}">${a.reconciled ? '已核对：各组已实现与浮动损益之和等于账户总盈亏。' : '归因与账户总盈亏待核对，请勿据此判优。'}</p>${table(a.by_strategy)}<details><summary>按入场版本查看</summary>${table(a.by_version)}</details><details><summary>策略与版本交叉查看</summary>${table(a.by_strategy_version)}</details><details><summary>按退出原因查看（仅已平仓）</summary>${table(a.by_exit)}</details><p class="muted">${esc(a.method)}</p></section>`;
  }
  if (study) {
    const blocked = study.blocked || {};
    markup += `<section class="panel research-panel" id="forward-study"><h2>弱市过滤 · 独立前向对照</h2><p role="status">${esc(study.message)}${study.activated_at ? ' · 启用 '+clockLabel(study.activated_at) : ''}${study.last_batch ? ' · 最新共同报价 '+clockLabel(study.last_batch) : ''} · 已观察 ${study.batches || 0} 批</p><div class="research-grid">${(study.arms || []).map(r=>`<article class="research-card"><h3>${esc(r.label)}</h3><p>总资产 ${num(r.summary.equity)} 元 · 盈亏 ${num(r.summary.profit)} 元（${pct(r.summary.return)}）</p><p>现金 ${num(r.summary.available_cash)} · 持仓市值 ${num(r.summary.market_value)} 元</p><p>已平仓 ${r.summary.closed_trades || 0} / 持仓 ${r.summary.open_positions || 0} · 费用 ${num(r.summary.fees)} 元</p>${r.observations?.count ? `<p>有效报价观察最大回撤 ${pct(r.observations.max_drawdown)} · 平均仓位 ${pct(r.observations.exposure_sum / r.observations.count * 100)}</p>` : ""}${r.stop ? `<p class="error">${esc(r.stop.reason)}</p>` : ''}${r.status === 'partial' ? '<p class="error">部分行情待核验，保留最近有效估值</p>' : ''}</article>`).join('')}</div>${study.difference != null ? `<p>过滤组资产减对照组资产：<b>${num(study.difference)} 元</b>。正数表示过滤组较高；样本尚少，不自动判优。</p>` : ''}<details><summary>被过滤挡住的对照组买入 · ${blocked.count || 0} 笔</summary><p>这些交易在对照组中的已实现损益 ${num(blocked.realized)} 元，当前浮动损益 ${num(blocked.unrealized)} 元；正数代表错过的盈利，负数代表避开的亏损。未核验 ${blocked.missing || 0} 笔。下列展示最近100笔，合计涵盖全部。</p><div class="table-wrap"><table><thead><tr><th>股票</th><th>买入日</th><th>状态</th><th>对照组损益（元）</th></tr></thead><tbody>${(blocked.items || []).map(r=>`<tr><td>${esc(r.name)} ${esc(r.code)}</td><td>${dateLabel(r.entry_day)}</td><td>${({closed:'已平仓',open:'持仓中',unavailable:'待核验'})[r.status]}</td><td>${num(r.profit)}</td></tr>`).join('')}</tbody></table></div></details><p class="muted">${esc(study.method)}</p></section>`;
  }
  return markup;
}
function renderBacktest(data) {
  const openSources = GuanshiEvidence.remember($("backtest-output"));
  if (data.trade_plan) renderTradePlan(data.trade_plan);
  rememberTradeEvidence();
  if ($("fill-history")) fillHistoryOpen = $("fill-history").open;
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
  const syncNotice = OFFLINE?.public && OFFLINE.portfolio && /^https?:$/.test(location.protocol) ? `<div id="portfolio-sync-status" class="notice${publicSyncState.status === "failed" ? " error" : ""}" role="status"><span>${esc(publicSyncState.text)}</span><button type="button" class="button secondary" data-action="retry-live">重新同步</button><a href="../portfolio.html?v=${Date.now()}">打开最新持仓</a></div>` : "";
  const valuationNotice = valuation ? `<div class="notice${["stale","unavailable","partial"].includes(valuation.status) ? " error" : ""}"><b>${data.paper_trading ? "模拟账户实时状态" : live ? "盘中持仓估值" : "日线账户估值"}</b><span>${esc(valuation.message)}${live ? ` 行情 ${timeLabel(valuation.quote_start)}～${timeLabel(valuation.quote_end)} · 采集 ${timeLabel(valuation.captured_at)}` : ""} · 交易时段每${valuation.interval_minutes}分钟采集${OFFLINE ? " · 在线持仓独立同步" : ""}</span></div>` : "";
  const p = data.parameters || {};
  const last = curve[curve.length - 1];
  const initial = s.initial_capital ?? p.capital ?? null;
  const cash = s.available_cash ?? last?.cash ?? null;
  const equity = s.equity ?? last?.equity ?? null;
  const marketValue = s.market_value ?? (equity !== null && cash !== null ? equity - cash : null);
  const profit = s.profit ?? (equity !== null && initial !== null ? equity - initial : null);
  const dailyBasis = s.daily_profit_basis_day ? `${dateLabel(s.daily_profit_basis_day)}收盘净值` : "初始资金";
  const accountCards = `<div id="simulation-account" class="tracking-grid funds-grid">${[
    ["initial_capital","初始资金",initial,"本次模拟的起始资金"],
    ["market_value","当前持仓市值",marketValue,`按最近有效估值计算 · ${s.open_positions || 0}只持仓`],
    ["available_cash","可用资金",cash,"扣除已发生费用后的现金余额"],
    ["daily_profit","当日盈亏",s.daily_profit,`${dateLabel(s.daily_profit_day || s.through)} · ${pct(s.daily_return)} · 较${dailyBasis}`],
    ["profit","累计盈亏",profit,`${pct(s.cumulative_return ?? s.return)} · 总资产减初始资金，已计入发生的费用`],
  ].map(([field,label,value,note]) => `<div class="tracking-card" data-account-field="${field}"><h3>${label}</h3><div class="metric-value ${["profit","daily_profit"].includes(field) ? color(value) : ""}">${["profit","daily_profit"].includes(field) && value > 0 ? "+" : ""}${num(value)}<small> 元</small></div><p>${esc(note)}</p></div>`).join("")}</div><p class="muted">总资产 ${num(equity)} 元 = 持仓市值 + 可用资金 · 账户估值截至 ${dateLabel(s.through || last?.day)}。当日盈亏为账户净值变化，包含持仓涨跌及当日成交费用，不等同于卖出交易的整轮盈亏。</p>`;
  const parameterText = `资金 ${num(p.capital,0)}元 · 单只权重 ${num(p.weight * 100,0)}% · 最多${p.max_positions}只 · 持有${p.holding_days}交易日 · 每边佣金${num(p.commission_bps,1)}基点（最低${num(p.min_commission)}元） · 卖出税费${num(p.sell_tax_bps,1)}基点 · 每边过户费${num(p.transfer_bps,1)}基点 · 每边滑点${num(p.slippage_bps,1)}基点 · 前一日量能参与上限${num(p.participation * 100,1)}%`;
  const riskText = `单笔风险预算 ${p.risk_fraction ? num(p.risk_fraction * 100,2) + "%（估算）" : "关闭"} · ATR风险距离下限 ${num(p.atr_risk_floor ?? 0,1)}倍 · 开盘追高上限 ${p.max_entry_gap ? pct(p.max_entry_gap * 100) : "关闭"} · 失效入场保护 ${p.require_valid_entry ? "开启" : "关闭"} · 行业上限 ${p.max_industry_weight ? pct(p.max_industry_weight * 100) : "关闭"} · 市场过滤 ${p.market_filter ? "开启" : "关闭"} · 回撤暂停 ${p.drawdown_pause ? pct(p.drawdown_pause * 100) : "关闭"}`;
  const metrics = data.performance;
  const intradayEvaluations = (data.entry_policy?.candidates || []).filter(e=>e.origin === "intraday");
  const intradayPool = intradayEvaluations.length ? `<details class="panel"><summary>盘中新发现的入场进度 · ${intradayEvaluations.length} 条</summary><div class="research-grid">${intradayEvaluations.map(e=>`<article class="research-card"><h3>${esc(e.name)} · ${esc(e.code)}</h3>${entryObservation(e)}</article>`).join("")}</div></details>` : "";
  const policyNotice = (data.entry_policy ? `<div class="notice"><span>盘中买入 ${esc(data.entry_policy.version)} · 重评 ${clockLabel(data.entry_policy.evaluated_at)}。${esc(data.entry_policy.method)}</span></div>` : data.entry_policy_config ? `<div class="notice"><span>盘中买入 ${esc(data.entry_policy_config.version)} 已配置。下一批有效报价起积累观察数据，连续确认后才尝试买入。${esc(data.entry_policy_config.method)}</span></div>` : "") + (data.exit_policy ? `<div class="notice"><span>退出保护 ${esc(data.exit_policy.version)}：盘中止损、2R后移动保护、连续两日收盘低于MA20退出、10交易日兜底。${exitInitialRule(data.exit_policy)}。启用 ${clockLabel(data.exit_policy.activated_at)}；已有持仓按每笔保存版本管理，保护价不重算；高点从启用后有效报价记录。</span></div>` : data.exit_policy_config ? `<div class="notice"><span>退出保护 ${esc(data.exit_policy_config.version)} 已配置：盘中止损、2R后移动盈利保护、连续两日收盘低于MA20退出、10交易日兜底。${exitInitialRule(data.exit_policy_config)}。已有持仓按每笔保存版本管理，保护价不重算；午休期间不确认成交。</span></div>` : "") + (data.policy?.effective_from ? `<div class="notice"><span>默认账户从 ${dateLabel(data.policy.effective_from)} 起采用风险保护规则；此前成交和既有持仓沿用原记录，现有持仓的退出保护以每笔保存版本为准。</span></div>` : "");
  const performanceCards = metrics ? `<div class="tracking-grid">${[
    ["日线夏普",num(metrics.sharpe),"无风险利率假设为0；至少30个日收益"],
    ["已平仓胜率",pct(metrics.win_rate),`盈亏比 ${num(metrics.payoff_ratio)} · 盈利因子 ${num(metrics.profit_factor)}`],
    ["最长未恢复净值高点",`${metrics.longest_underwater_sessions}交易日`,`目前连续 ${metrics.current_underwater_sessions}交易日`],
    ["年化净收益",pct(metrics.annual_return),"至少252个日收益；短样本留空"],
    ["累计双边换手",num(metrics.turnover) + "倍","成交额 / 平均净值，未年化"],
    ["模拟滑点成本",num(metrics.slippage_cost) + "元","已反映在成交价与盈亏中，不重复扣除"],
  ].map(([label,value,note]) => `<div class="tracking-card"><h3>${label}</h3><div class="metric-value">${value}</div><p>${esc(note)}</p></div>`).join("")}</div><p class="muted">${metrics.small_sample ? "样本不足，尚不能判定策略优势。 " : ""}绩效按日线账户统计，盘中估值不改变这些指标。</p>` : "";
  tradeRows = [...(data.trades || []).map((t) => ({...t,status:"closed"})), ...positions.map((t) => ({...t,status:"open"}))]
    .sort((a,b) => (a.status === b.status ? String(b.exit_day || b.entry_day).localeCompare(String(a.exit_day || a.entry_day)) || a.code.localeCompare(b.code) : a.status === "open" ? -1 : 1));
  tradeCutoff = s.through || data.summary.through;
  tradeHoldingDays = p.holding_days;
  fillRows = data.fills ?? legacyFillRows(data);
  const fillHistory = `<details id="fill-history" class="panel research-panel"${fillHistoryOpen ? " open" : ""}><summary>历史全部成交 · ${fillRows.length} 笔</summary><div class="research-body">
    <p class="muted">每次已模拟成交的买入、卖出分别列一笔，最新在前；涵盖当前模拟方案截至所选日期的全部历史。历史开盘回放与盘中报价模拟分别标注；盘中记录保存报价及确认时间。</p>
    <div class="fill-filters"><label>搜索名称、代码或策略<input id="fill-search" type="search" placeholder="搜索全部历史成交"></label><label>成交方向<select id="fill-side"><option value="all">全部方向</option><option value="buy">仅买入</option><option value="sell">仅卖出</option></select></label><label>起始日期<input id="fill-from" type="date"></label><label>结束日期<input id="fill-to" type="date"></label><label>每页笔数<select id="fill-size"><option value="20">20</option><option value="50">50</option><option value="100">100</option></select></label></div>
    <div class="research-actions"><button id="fill-reset" type="button" class="button secondary">清除筛选</button><button id="export-fills" type="button" class="button secondary">导出全部筛选成交 CSV</button></div><p id="fill-count" class="muted" role="status"></p>
    <div class="table-wrap"><table><thead><tr><th>成交日期</th><th>股票</th><th>方向</th><th>成交价（元）</th><th>股数</th><th>成交额（元）</th><th>费用（元）</th><th>现金变化（元）</th><th>已实现盈亏（元）</th><th>详情</th></tr></thead><tbody id="fill-table"></tbody></table></div>
    <div class="fill-pagination"><button id="fill-prev" type="button" class="button secondary">上一页</button><span id="fill-page" aria-live="polite"></span><button id="fill-next" type="button" class="button secondary">下一页</button></div></div></details>`;
  const ledger = `<section class="panel research-panel"><div class="panel-heading"><div><h2>买卖时间与价格</h2><p>持仓优先、最近交易在前；北京时间；历史开盘回放与盘中报价模拟分别标注，价格已计滑点。</p></div></div>
    <div class="table-toolbar trade-toolbar"><label class="search"><input id="trade-search" type="search" aria-label="搜索模拟交易" placeholder="搜索名称、代码或策略" value="${esc(tradeSearch)}"></label><select id="trade-status" aria-label="模拟交易状态"><option value="all">全部交易</option><option value="open">当前持仓 / 待退出</option><option value="closed">已卖出</option></select><button id="export-trades" type="button" class="button secondary">导出当前明细 CSV</button></div>
    <p id="trade-count" class="muted trade-count"></p><div id="trade-ledger" class="trade-grid"></div></section>`;
  let chart = "";
  if (curve.length > 1) {
    const values = curve.flatMap((r) => [r.return,r.benchmark_return ?? 0]), low = Math.min(...values), span = Math.max(...values) - low || 1;
    const points = (key) => curve.map((r,i) => `${num(20 + i/(curve.length-1)*560)},${num(140 - ((r[key] ?? 0)-low)/span*110)}`).join(" ");
    chart = `<svg class="price-chart" viewBox="0 0 600 165" role="img" aria-label="模拟组合与沪深300累计变化"><polyline class="chart-line" points="${points("return")}"/><polyline fill="none" stroke="#b18c54" stroke-width="2" points="${points("benchmark_return")}"/></svg><p class="muted">绿色：模拟组合 · 棕色：沪深300 · ${dateLabel(curve[0].day)} → ${dateLabel(curve[curve.length-1].day)}${live ? " · 最后一点为所标时点的盘中估值" : ""}</p>`;
  }
  $("backtest-output").innerHTML = `${syncNotice}${valuationNotice}${policyNotice}${intradayPool}${data.stop ? `<div class="notice error">${esc(data.stop.reason)} · ${dateLabel(data.stop.day)}；净值仅计算至 ${dateLabel(data.summary.through)}</div>` : ""}${accountCards}${researchPanels(data)}${analysisMarkup(data.market_analysis,"AI盘中研判与持仓参考","portfolio-ai")}${decisionMarkup(data.decision_context?.enabled ? data.decision_context : data.decision_policy || data.decision_context)}
    <p class="muted">已实现盈亏 ${num(s.realized_profit)} 元 · 持仓浮盈亏 ${num(s.unrealized_profit)} 元；两者合计为账户盈亏，浮盈亏仅扣已发生买入费用。</p><div class="research-actions"><button id="show-fill-history" type="button" class="button primary">查看全部历史成交（${fillRows.length}笔）</button></div>${fillHistory}${ledger}
    <div class="tracking-grid"><div class="tracking-card"><h3>模拟组合累计变化</h3><div class="metric-value">${pct(s.return)}</div><p>沪深300同期 ${pct(s.benchmark_return)}</p></div><div class="tracking-card"><h3>日线组合最大回撤</h3><div class="metric-value">${pct(s.max_drawdown)}</div><p>已卖出 ${s.closed_trades || 0} / 未平仓 ${s.open_positions || 0}</p></div><div class="tracking-card"><h3>累计模拟费用</h3><div class="metric-value">${num(s.fees)} 元</div><p>未平仓费用只含已发生的入场费用</p></div></div>${chart}
    ${performanceCards}<details class="panel research-panel"><summary>模拟规则与参数</summary><div class="research-body"><p>${esc(data.method)}</p><p>${esc(parameterText)}${OFFLINE ? " · 离线快照固定参数，需在本地服务中修改" : ""}</p><p>${esc(riskText)}</p></div></details>
    <details class="panel research-panel"><summary>未成交与退出延期原因</summary><div class="research-body research-grid">${Object.entries(data.skipped || {}).map(([reason,count]) => `<div class="research-card">${esc(reason)}：${count}</div>`).join("") || "暂无跳过记录"}</div></details>
    ${(data.periods || []).map((p) => `<p>${esc(p.label)} ${dateLabel(p.from)} → ${dateLabel(p.to)}：${pct(p.return)}</p>`).join("")}`;
  GuanshiEvidence.restore($("backtest-output"),openSources);
  $("trade-status").value = tradeStatus;
  renderTradeLedger();
  $("trade-search").addEventListener("input", (e) => { tradeSearch = e.target.value; renderTradeLedger(); });
  $("trade-status").addEventListener("change", (e) => { tradeStatus = e.target.value; renderTradeLedger(); });
  $("export-trades").addEventListener("click", exportTradeLedger);
  setupFillHistory();
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
const profileLabel = (p) => ({baseline:"原版基线",guarded:"风险保护版",adaptive:"市场过滤版"}[p] || p);
let researchId = 0;
async function loadResearch(kind) {
  const id = ++researchId, day = state?.report?.day;
  if (!day || OFFLINE) return;
  const buttons = [$("research-compare"),$("research-walk")];
  buttons.forEach((b) => b.disabled = true);
  $("research-output").textContent = "正在核验全部方案与样本，请稍候…";
  try {
    const data = await api(`/api/research/${kind}?day=${encodeURIComponent(day)}`);
    if (id !== researchId || day !== state?.report?.day) return;
    const status = {insufficient:"样本不足",incomplete:"数据不完整",evaluated:"研究评估已完成"}[data.status] || data.status;
    const rows = (items) => items.map((r) => `<tr><td>${esc(r.label || profileLabel(r.profile))}</td><td>${pct(r.summary?.return)}</td><td>${pct(r.summary?.max_drawdown)}</td><td>${r.summary?.closed_trades ?? 0}</td><td>${num(r.performance?.sharpe)}</td><td>${esc(r.status === "incomplete" ? "数据不完整" : r.performance?.small_sample ? "样本不足" : "研究结果")}</td></tr>`).join("");
    const table = (items) => `<div class="table-wrap"><table><thead><tr><th>方案 / 实验</th><th>扣费累计变化</th><th>最大回撤</th><th>平仓笔数</th><th>夏普</th><th>样本状态</th></tr></thead><tbody>${rows(items)}</tbody></table></div>`;
    $("research-output").innerHTML = `<div class="notice${data.status === "evaluated" ? "" : " error"}"><b>${esc(status)}</b><span>${esc(data.message)} 已有${data.session_count || 0}交易日${data.required_sessions ? `，首个完整窗口需要${data.required_sessions}交易日` : ""}。</span></div>
      <p class="muted">数据截至 ${dateLabel(data.as_of)} · 本次研究使用固定默认参数，与上方自定义模拟参数独立。</p>
      ${data.profiles ? table(data.profiles) + data.profiles.map((p) => `<details class="panel research-panel"><summary>${esc(profileLabel(p.profile))}：成本压力与持有期敏感性</summary>${table(p.stress_tests)}</details>`).join("") : ""}
      ${(data.folds || []).map((f) => `<details class="panel research-panel"><summary>测试 ${dateLabel(f.test_start)} → ${dateLabel(f.test_end)} · ${f.selected_profile ? esc(profileLabel(f.selected_profile)) : "训练样本不足，未选方案"}</summary><div class="research-body"><p>训练 ${dateLabel(f.train_start)} → ${dateLabel(f.train_end)}；隔离${f.purge_sessions}交易日。</p>${table(f.training_trials)}${f.test_results.length ? table(f.test_results) : "未使用不足的训练样本评选方案。"}</div></details>`).join("")}
      <p class="muted">${(data.limitations || []).map(esc).join(" ")}</p>`;
    const download = document.createElement("button");
    download.type = "button"; download.className = "button secondary"; download.textContent = "下载本次研究记录 JSON";
    download.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));
      const link = document.createElement("a"); link.href = url; link.download = `策略研究-${kind}-${day}.json`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
    });
    $("research-output").append(download);
  } catch (error) {
    if (id === researchId && day === state?.report?.day) $("research-output").textContent = error.message;
  } finally { if (id === researchId) buttons.forEach((b) => b.disabled = !!OFFLINE); }
}
$("research-compare").addEventListener("click", () => loadResearch("compare"));
$("research-walk").addEventListener("click", () => loadResearch("walk-forward"));
if (OFFLINE) [$("research-compare"),$("research-walk")].forEach((b) => b.disabled = true);
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
  document.querySelectorAll(".nav-item").forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle("active", active);
    if (active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  document.querySelector(".page-heading .eyebrow").textContent = {overview:"AFTER THE CLOSE",watch:"YOUR WATCHLIST",tracking:"SIGNAL TRACKING",backtest:"PAPER PORTFOLIO"}[view];
  document.querySelectorAll(".view").forEach((s) => s.hidden = s.id!==`view-${view}`);
  if (view === "tracking") loadTracking();
  if (view === "backtest") loadBacktest();
}
document.querySelectorAll(".nav-item[data-view]").forEach((b) => b.addEventListener("click",() => switchView(b.dataset.view)));
document.querySelectorAll("[data-strategy]").forEach((b) => b.addEventListener("click",() => {strategy=b.dataset.strategy;document.querySelectorAll("[data-strategy]").forEach((s) => {s.classList.toggle("active",s===b);s.setAttribute("aria-pressed",String(s===b));});renderCandidates();}));
$("candidate-search").addEventListener("input",renderCandidates);
$("candidate-scope").addEventListener("change",renderCandidates);
$("candidate-table").addEventListener("click",(e) => {const b=e.target.closest(".row-detail");if(b)showDetail(b.dataset.code,b.dataset.rule);});
$("date-select").addEventListener("change",() => {selectedDay = $("date-select").value; load();});
$("refresh").addEventListener("click",() => OFFLINE?.live_url && /^https?:$/.test(location.protocol) ? refreshPublicPortfolio() : OFFLINE ? window.location.reload() : load());
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
load().then(() => { if (OFFLINE?.portfolio) { switchView("backtest"); refreshPublicPortfolio(); } else if (!OFFLINE && new URLSearchParams(location.search).get("view") === "backtest") switchView("backtest"); });
setInterval(() => {
  if (!OFFLINE && state && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled && !$("backtest-form").querySelector("button").disabled) load();
}, 30000);

if (OFFLINE?.portfolio && OFFLINE.public && !OFFLINE.live_url && /^https?:$/.test(location.protocol)) {
  setInterval(() => { if (!document.hidden) location.replace(new URL("../portfolio.html", location.href)); }, 600000);
}

let publicPortfolioPending = false;
function updatePublicSync(status,text) {
  publicSyncState = {status,text};
  const notice = $("portfolio-sync-status");
  if (notice) { notice.querySelector("span").textContent = text; notice.className = `notice${status === "failed" ? " error" : ""}`; }
}
function publicQuoteExpired(data) {
  const v = data?.backtest?.valuation;
  if (!v || !["available","stale"].includes(v.status)) return false;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).formatToParts(new Date()).map((p) => [p.type,p.value]));
  const today = `${parts.year}${parts.month}${parts.day}`, clock = `${parts.hour}:${parts.minute}`;
  const cutoff = clock > "15:00" ? "15:00" : clock > "11:30" && clock < "13:00" ? "11:30" : clock;
  const expectedAt = Date.parse(`${parts.year}-${parts.month}-${parts.day}T${cutoff}:00+08:00`);
  return v.status === "stale" || today !== v.day || expectedAt - Date.parse(v.quote_start) > (v.interval_minutes + 5) * 60000;
}
async function fetchPublicPortfolio(url, backup = false) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),6000);
  try {
    url.searchParams.set("t",String(Date.now()));
    const response = await fetch(url,{cache:"no-store",credentials:"omit",signal:controller.signal,
      headers:backup ? {Accept:"application/vnd.github.raw+json"} : {}});
    if (!response.ok) throw new Error("同步未成功");
    const data = await response.json();
    if (data.version !== "portfolio-live-v1" || data.source !== OFFLINE.dashboard.source || !data.backtest?.summary || !Number.isFinite(Date.parse(data.data_at))) throw new Error("数据格式无效");
    return data;
  } finally { clearTimeout(timeout); }
}
async function refreshPublicPortfolio() {
  if (!OFFLINE?.public || !OFFLINE.portfolio || !/^https?:$/.test(location.protocol) || publicPortfolioPending) return;
  if (!OFFLINE.live_url) { updatePublicSync("failed","此页面尚未启用实时读取，请打开最新持仓入口。"); return; }
  publicPortfolioPending = true;
  updatePublicSync("pending","正在同步最新持仓…");
  try {
    const url = new URL(OFFLINE.live_url);
    let data, backup = false;
    try { data = await fetchPublicPortfolio(new URL(url)); } catch (_) {}
    const previousAt = OFFLINE.backtest?.valuation?.captured_at;
    const oldResponse = data && previousAt && Date.parse(data.data_at) < Date.parse(previousAt);
    if ((!data || oldResponse || publicQuoteExpired(data)) && Date.now() - lastPublicApiAttempt >= 120000) {
      const parts = url.pathname.split("/").filter(Boolean);
      if (url.hostname === "raw.githubusercontent.com" && parts.length === 4) {
        lastPublicApiAttempt = Date.now();
        const apiUrl = new URL(`https://api.github.com/repos/${parts[0]}/${parts[1]}/contents/${parts[3]}`);
        apiUrl.searchParams.set("ref",parts[2]);
        try { data = await fetchPublicPortfolio(apiUrl,true); backup = true; } catch (_) {}
      }
    }
    if (!data) throw new Error("实时同步未成功，当前显示保存的估值；可点击重新同步或打开最新持仓。");
    if (previousAt && Date.parse(data.data_at) < Date.parse(previousAt)) {
      updatePublicSync("failed",`最新读取仍落后于当前文件，保留 ${timeLabel(previousAt)} 的估值，稍后重试。`);
      return;
    }
    OFFLINE.backtest = data.backtest;
    if (data.trade_plan) {
      OFFLINE.dashboard.report.trade_plan = data.trade_plan;
      renderTradePlan(data.trade_plan);
    }
    updatePublicSync(publicQuoteExpired(data) ? "failed" : "success",`${publicQuoteExpired(data) ? "行情仍过期，保留保存的估值" : "已同步"} · 行情 ${timeLabel(data.data_at)} · 检查 ${timeLabel(new Date().toISOString())}${backup ? " · 备用通道" : ""}`);
    if (currentView === "backtest") renderBacktest(data.backtest);
    message(`行情采集 ${timeLabel(data.data_at)} · 公开同步 ${timeLabel(data.published_at)} · 页面每分钟检查更新。`);
  } catch (error) { updatePublicSync("failed",error.message); message(error.message,true); }
  finally { publicPortfolioPending = false; }
}
$("backtest-output").addEventListener("click",(e) => {if (e.target.closest('[data-action="retry-live"]')) refreshPublicPortfolio();});
setInterval(() => { if (!document.hidden) refreshPublicPortfolio(); },60000);
document.addEventListener("visibilitychange",() => { if (!document.hidden) refreshPublicPortfolio(); });

window.addEventListener("pageshow",() => { if (!document.hidden) refreshPublicPortfolio(); });
window.addEventListener("focus",() => { if (!document.hidden) refreshPublicPortfolio(); });
