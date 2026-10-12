"use strict";
const data = JSON.parse(document.getElementById("quote-snapshot").textContent);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, digits=2) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {minimumFractionDigits:digits, maximumFractionDigits:digits});
const pct = v => v == null ? "—" : `${v > 0 ? "+" : ""}${num(v)}%`;
const pp = v => v == null ? "—" : `${v > 0 ? "+" : ""}${num(v)} pp`;
const color = v => v > 0 ? "red" : v < 0 ? "green" : "";
const el = id => document.getElementById(id);
const empty = text => `<tr><td colspan="6" class="empty">${esc(text)}</td></tr>`;
const a = data.analysis || {summaries:["该快照尚未生成量价分析，可用 export-snapshot 重新导出。"], notes:[], hot_sectors:[], counts:{}, volume_up:[], volume_down:[], risk_alerts:[]};
const m = data.market;
const cards = [
  ["沪深300", pct(m.benchmark.pct), color(m.benchmark.pct), `最新 ${num(m.benchmark.price)} · ${m.benchmark.quote_time.slice(11,19)}`],
  ["上涨股票占比", `${num(m.up/m.count*100,1)}%`, "", `${m.up} 上涨 / ${m.down} 下跌 / ${m.flat} 平盘`],
  [data.slot === "midday" ? "上午累计成交额" : "当日累计成交额", `${num(m.amount/1e8,0)} 亿`, "", "有效报价样本 · 未外推全天成交额"],
  ["量能可比样本", `${num(a.volume_coverage || 0,0)} 只`, "", data.slot === "midday" ? "至少5日同时间样本 · 缺少时不判断缩量" : "连续20个交易日 · 排除复权或昨收异常"]
];
el("metrics").innerHTML = cards.map(([label,value,cls,sub]) => `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value ${cls}">${esc(value)}</div><p>${esc(sub)}</p></div>`).join("");
el("brief").innerHTML = a.summaries.map(s => `<li>${esc(s)}</li>`).join("");
el("method").innerHTML = a.notes.map(s => `<li>${esc(s)}</li>`).join("");
function leaders(s) { return (s.leaders || []).map(r => `${esc(r.name)} <span class="${color(r.pct)}">${pct(r.pct)}</span>`).join(" · "); }
function afternoon(s) {
  const r = s.afternoon;
  if (!r) return '<small>午间样本缺失或不可比</small>';
  const label = r.return >= 1 ? "午后走强" : r.return <= -1 ? "午后降温" : "午后变化较小";
  return `<b class="${color(r.return)}">${label} ${pct(r.return)}</b><small>午后上涨 ${num(r.up_ratio,0)}% · 成交占比 ${num(r.amount_share)}%</small>`;
}
el("hotspots").innerHTML = a.hot_sectors.length ? a.hot_sectors.map(s => `<article class="hot-card"><div class="hot-heading"><h3>${esc(s.name)}</h3><span class="tag">${esc(s.status)}</span></div><div class="hot-value ${color(s.pct)}">${pct(s.pct)}</div><p>${s.count}只样本 · 上涨 ${num(s.up_ratio,0)}%<br>成交额占比 ${num(s.amount_share)}%${s.share_change_pp == null ? "" : ` · 较昨日 ${pp(s.share_change_pp)}`}</p><p>可比量能 ${s.volume_count}只 · 放量 ${s.volume_up_count} / 缩量 ${s.volume_down_count}</p><div class="continuity">${s.history_compared ? `已观察到连续 ${s.active_streak} 个交易日同时间活跃` : "同时间历史不足，延续性待观察"}</div>${data.slot === "close" ? `<div class="afternoon">${afternoon(s)}</div>` : ""}<div class="leaders">${leaders(s)}</div></article>`).join("") : '<p class="empty">暂无满足广度与相对强度条件的活跃行业。下表保留分化和转弱信息。</p>';
el("sectors").innerHTML = data.industries.slice(0,12).map(s => `<tr><td><b>${esc(s.name)}</b><small>${s.count}只 · ${esc(s.status || "")}</small></td><td><b class="${color(s.pct)}">${pct(s.pct)}</b><small>上涨 ${num(s.up_ratio,0)}%</small></td><td>${num(s.amount_share)}%<small>${s.share_change_pp == null ? "占比变化暂无可比样本" : `较昨日 ${pp(s.share_change_pp)}`}</small></td><td>${s.pct_change_pp == null ? "—" : `涨跌变化 ${pp(s.pct_change_pp)}`}<small>${s.breadth_change_pp == null ? "同时间历史缺失或样本不可比" : `上涨广度 ${pp(s.breadth_change_pp)}`}</small></td><td>${data.slot === "close" ? afternoon(s) : leaders(s)}</td></tr>`).join("") || '<tr><td colspan="5">暂无已分类行业样本。</td></tr>';
function positions(r) {
  if (r.ma20 == null) return '<span class="muted">历史/复权参考不可用</span>';
  return `20日均线 ${num(r.ma20)}<small>距均线 ${pct(r.distance_ma20)}</small><small>前20日高点 ${num(r.prior_high20)} · 距高点 ${pct(r.distance_high20)}</small>`;
}
function volume(r) {
  if (r.volume_ratio != null) return `<b>${num(r.volume_ratio)} ×</b><small>${esc(r.volume_baseline)}</small>`;
  return `不可比<small>${data.slot === "midday" ? `同时间样本 ${r.volume_samples || 0}/至少5日` : "需连续20日有效历史"}</small>${data.slot === "midday" && r.daily_volume_ratio != null ? `<small>上午量 / 历史全天均量 ${num(r.daily_volume_ratio)}×</small>` : ""}`;
}
function observedRow(r, focus=false) {
  const tags = (r.tags || []).map(t => `<span class="tag ${/下跌|回落|跌破/.test(t) ? "risk" : ""}">${esc(t)}</span>`).join("") || '<small>当前检查项未触发异动</small>';
  const change = data.slot === "close" ? (r.afternoon ? `<b class="${color(r.afternoon.return)}">午后 ${pct(r.afternoon.return)}</b><small>新增成交 ${(num(r.afternoon.amount/1e8))} 亿</small>` : '<small>午后比较不可用</small>') : `较最高价回落 ${num(r.fade_pp)} pp`;
  return `<tr><td><b>${esc(r.name)}</b><small>${esc(r.code)} · ${esc(r.industry)}</small>${focus ? `<small>${esc(r.reason)}</small>` : ""}</td><td>${num(r.price)}<small class="${color(r.pct)}">${pct(r.pct)}</small><small>报价 ${esc(r.quote_time.slice(11,19))}</small></td><td class="tag-cell">${tags}</td><td>${volume(r)}</td><td>${positions(r)}</td><td class="risk-cell">${change}${(r.risks || []).map(t => `<small>${esc(t)}</small>`).join("")}</td></tr>`;
}
function spotlight(rows, fallback) {
  return rows.length ? rows.slice(0,4).map(r => `<div class="spotlight-row"><div><b>${esc(r.name)}</b> <span class="${color(r.pct)}">${pct(r.pct)}</span><small>${esc(r.code)} · ${esc(r.industry)}</small></div><div>${(r.tags || []).map(esc).join(" · ")}<small>${r.volume_ratio == null ? `较最高价回落 ${num(r.fade_pp)} pp` : `${num(r.volume_ratio)}× · ${esc(r.volume_baseline)}`}</small></div></div>`).join("") : `<p class="hint">${esc(fallback)}</p>`;
}
el("shrink-preview").innerHTML = spotlight(a.volume_down,"暂无可确认的缩量观察；同时间样本不足时不判断缩量。");
el("risk-preview").innerHTML = spotlight(a.risk_alerts,"当前检查项未发现转弱或明显回落，不代表没有其他风险。");
el("volume-coverage").textContent = `${a.volume_coverage || 0} / ${m.count} 只量能可比`;
el("up-count").textContent = a.counts.volume_up || 0;
el("down-count").textContent = a.counts.volume_down || 0;
el("risk-count").textContent = a.counts.risk || 0;
let kind = "volume_up";
function renderSignals() {
  const rows = a[kind] || [];
  const messages = {volume_up:"未发现满足成交额与量能条件的放量异动。",volume_down:"未发现可确认的缩量异动；午间历史不足时不会判断缩量。",risk_alerts:"当前检查项未发现风险异动；不代表不存在其他风险。"};
  el("signals").innerHTML = rows.length ? rows.map(r => observedRow(r)).join("") : empty(messages[kind]);
  const total = a.counts[kind === "risk_alerts" ? "risk" : kind] || 0;
  el("signal-count").textContent = `符合本类条件 ${total} 只，展示 ${rows.length} 只。放量按倍数、缩量按低倍数、风险按触发项与回落幅度排序；均非买卖优先级。`;
  document.querySelectorAll("#signal-tabs button").forEach(b => {b.classList.toggle("active",b.dataset.kind === kind);b.setAttribute("aria-pressed",String(b.dataset.kind === kind));});
}
el("signal-tabs").addEventListener("click", e => {const b = e.target.closest("button[data-kind]");if (b) {kind=b.dataset.kind;renderSignals();}});
renderSignals();
el("focus").innerHTML = data.focus.length ? data.focus.map(r => observedRow(r,true)).join("") : empty("暂无自选股或更早日线信号的有效报价。");
function quoteRow(r) { return `<tr><td><b>${esc(r.name)}</b><small>${esc(r.code)}</small></td><td>${esc(r.industry)}</td><td>${num(r.price)}</td><td class="${color(r.pct)}">${pct(r.pct)}</td><td>${num(r.amount/1e8)} 亿</td><td>${esc(r.quote_time.slice(11,19))}</td></tr>`; }
function renderQuotes() {
  const query = el("search").value.trim().toLowerCase();
  const matches = data.quotes.filter(r => !query || `${r.code} ${r.name} ${r.industry}`.toLowerCase().includes(query));
  el("quotes").innerHTML = matches.slice(0,40).map(quoteRow).join("") || empty("没有匹配的有效报价。");
  el("count").textContent = `匹配 ${matches.length} 只，当前展示前 ${Math.min(40,matches.length)} 只。`;
}
el("search").addEventListener("input", renderQuotes);
renderQuotes();
const news = data.news || {status:"not_collected",items:[],topics:[],sources:[],notes:[],count:0};
const statusLabels = {available:"快讯已采集",partial:"部分来源可用",unavailable:"采集不可用 / 已过期",not_collected:"该时点尚未采集",disabled:"新闻未启用"};
const sourceLabels = {available:"接口正常",empty:"返回为空",partial:"部分字段无效",unavailable:"接口不可用",stale:"采集记录较早",not_collected:"尚未采集"};
function newsLink(title, url) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname;
    if (["http:","https:"].includes(parsed.protocol) && (host === "cls.cn" || host.endsWith(".cls.cn") || host === "eastmoney.com" || host.endsWith(".eastmoney.com"))) return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(title)} ↗</a>`;
  } catch (_) {}
  return esc(title);
}
function timeLabel(value) {return value ? value.slice(5,16).replace("T"," ") : "—";}
el("news-status").textContent = statusLabels[news.status] || "状态待核对";
el("news-timing").textContent = news.as_of ? `${news.supplemental ? "新闻补充采集" : "新闻截止"} ${timeLabel(news.as_of)} · 行情快照 ${timeLabel(news.quote_cutoff)} · 观察此前24小时已留存的报道${news.supplemental ? "；原行情时间与价格保留，补充新闻不代表当时已知。" : "。"}` : "该版本没有同期新闻记录，重新导出不会用后来的消息补造当时的新闻。";
el("news-sources").innerHTML = news.sources.map(s => `<span>${esc(s.name)} · ${esc(sourceLabels[s.status] || s.status)}${s.fetched_at ? ` · ${timeLabel(s.fetched_at)} · ${s.count}条` : ""}${s.invalid_count ? ` · 无效${s.invalid_count}条` : ""}${s.error ? ` · ${esc(s.error)}` : ""}</span>`).join("");
function marketReferences(topic) {
  const references = topic.related_sectors.slice(0,2).map(s => `${esc(s.name)} <span class="${color(s.pct)}">${pct(s.pct)}</span> · 上涨 ${num(s.up_ratio,0)}%`).join("<br>");
  return references || "暂无对应行业行情映射";
}
el("news-topics").innerHTML = news.topics.map(t => `<article class="news-topic"><div class="hot-heading"><h3>${esc(t.name)}</h3><span class="tag">${t.count}条收录</span></div><p>最近发布 ${timeLabel(t.latest_at)}${t.new_count == null ? "" : ` · 较午间新增收录 ${t.new_count}条`}</p><div class="topic-market">行业参考：<br>${marketReferences(t)}</div>${t.mentioned_stocks.length ? `<p>直接提及：${t.mentioned_stocks.slice(0,3).map(r => `${esc(r.name)} <span class="${color(r.pct)}">${pct(r.pct)}</span>`).join(" · ")}</p>` : ""}<div class="topic-headline">${newsLink(t.headline,t.url)}</div>${t.after_quote_count ? `<small class="news-late">${t.after_quote_count}条发布晚于行情快照，后续反应待观察</small>` : ""}</article>`).join("") || `<p class="empty">${news.status === "disabled" ? "新闻功能未启用；合成示例不混入真实快讯。" : news.status === "not_collected" ? "截至行情快照时点，没有已留存的新闻记录。" : "当前窗口暂无已留存的相关主题报道，不能据此认定没有热点消息。"}</p>`;
let newsExpanded = false;
function newsRow(item) {
  const sources = item.sources.map(s => newsLink(s.name,s.url)).join(" · ");
  const direct = item.mentioned_stocks.slice(0,6).map(r => `${esc(r.name)}（${esc(r.code)}）<span class="${color(r.pct)}">${pct(r.pct)}</span>${r.volume_ratio == null ? "" : ` · ${num(r.volume_ratio)}×`}`).join(" · ");
  const references = item.related_sectors.slice(0,3).map(s => `${esc(s.name)} <span class="${color(s.pct)}">${pct(s.pct)}</span>（${esc(s.basis)}）`).join(" · ");
  return `<article class="news-entry"><div class="news-meta"><time>${timeLabel(item.published_at)}</time><span class="tag ${/风险|传闻/.test(item.kind) ? "risk" : ""}">${esc(item.kind)}</span>${item.new_since_midday ? '<span class="tag">较午间新增收录</span>' : ""}${item.after_quote ? '<span class="tag risk">晚于行情快照</span>' : ""}</div><h3>${newsLink(item.title,item.sources[0]?.url)}</h3><p class="news-excerpt">${esc(item.excerpt)}</p><div class="news-related">${direct ? `直接提及个股（行情快照）：${direct}<br>` : ""}${references ? `相关行业参考：${references}<br>` : ""}${item.risk_words.length ? `待核对关键词：${item.risk_words.map(esc).join(" / ")}<br>` : ""}${item.after_quote ? "发布时间晚于此处行情，不能用已有涨跌判断消息后的市场反应。" : "此处只对照快照量价，未验证新闻与涨跌的因果关系。"}</div><div class="news-origin">收录渠道：${sources} · ${item.topics.map(esc).join(" / ")}</div></article>`;
}
function renderNews() {
  const filter = el("news-filter").value;
  const query = el("news-search").value.trim().toLowerCase();
  const matches = news.items.filter(item => (filter !== "new" || item.new_since_midday === true) && (filter !== "risk" || item.risk_words.length > 0) && (!query || `${item.title} ${item.excerpt} ${item.topics.join(" ")} ${item.mentioned_stocks.map(s => `${s.name} ${s.code}`).join(" ")}`.toLowerCase().includes(query)));
  const visible = matches.slice(0,newsExpanded ? 25 : 8);
  el("news-items").innerHTML = visible.length ? visible.map(newsRow).join("") : '<p class="empty">没有匹配的已留存快讯。午间没有同期新闻时，“较午间新增”比较不可用。</p>';
  el("news-count").textContent = `本窗口相关收录 ${news.count}条，优先清单保存前${news.items.length}条，当前筛选${matches.length}条，展示${visible.length}条${news.new_count == null ? "；暂无同期午间记录可比较。" : `；较午间新增收录${news.new_count}条。`}`;
  el("news-more").hidden = matches.length <= 8;
  el("news-more").textContent = newsExpanded ? "收起更多快讯" : "展开更多快讯";
}
el("news-filter").addEventListener("change", renderNews);
el("news-search").addEventListener("input", renderNews);
el("news-more").addEventListener("click", () => {newsExpanded=!newsExpanded;renderNews();});
renderNews();
el("method").innerHTML += news.notes.map(s => `<li>${esc(s)}</li>`).join("");
window.prepareReportForExport = function () {
  document.body.classList.add("png-export");
  // Always export the unfiltered report, independently of interactive tabs.
  el("news-filter").value = "all";
  el("news-search").value = "";
  newsExpanded = false;
  renderNews();
  const head = el("signals").closest("table").querySelector("thead").outerHTML;
  const groups = [
    ["volume_up", "放量观察", a.counts.volume_up || 0],
    ["volume_down", "缩量观察", a.counts.volume_down || 0],
    ["risk_alerts", "风险异动", a.counts.risk || 0]
  ];
  el("export-signals").innerHTML = groups.map(([key,label,total]) => {
    const rows = a[key] || [];
    return `<section class="export-signal-section" data-kind="${key}"><div class="section-title"><h3>${label} · ${total}只</h3><span>展示已保存优先观察 ${rows.length}条</span></div><div class="table-wrap"><table>${head}<tbody>${rows.length ? rows.map(r => observedRow(r)).join("") : empty("暂无可确认的已保存观察；历史样本不足时不判断放缩量。")}</tbody></table></div></section>`;
  }).join("");
  el("signal-view").hidden = true;
  el("export-signals").hidden = false;
  el("quote-section").open = true;
  el("method-section").open = true;
  el("search").value = "";
  renderQuotes();
  el("quote-summary").textContent = `报价概览 · 展开前${Math.min(40,data.quotes.length)}只 / 共${data.quotes.length}只有效报价`;
  el("quote-section").querySelector(".quote-tools .hint").textContent = `本图展示涨幅排序前${Math.min(40,data.quotes.length)}只；其余${Math.max(0,data.quotes.length-40)}只可在HTML中搜索。`;
  el("count").textContent = `本图展示涨幅排序前${Math.min(40,data.quotes.length)}只，全部${data.quotes.length}只报价保存在HTML中，可搜索代码、名称或行业。图片不提供搜索和筛选。`;
};
el("interactive-report").hidden = false;
el("static-report").hidden = true;
window.REPORT_READY = true;
