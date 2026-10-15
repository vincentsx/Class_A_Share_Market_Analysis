"use strict";
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v,d=2) => v == null ? "—" : Number(v).toFixed(d);
const pct = (v) => v == null ? "—" : `${v>0?"+":""}${num(v)}%`;
const tone = (v) => v>0 ? "up" : v<0 ? "down" : "";
const time = (v) => !v ? "—" : new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(v));
const safeLink = (url) => {try{const u=new URL(url);return /^https?:$/.test(u.protocol)&&!u.username&&!u.password?u.href:"";}catch(_){return "";}};
let data = JSON.parse($("realtime-data").textContent), latestData=data, archive="", search="", category="all", newsLimit=24, loading=false, lastApiAttempt=0;
const endpoint = document.querySelector("main").dataset.endpoint;
function newsCards(report) {
  const items=report.news.items.filter(r=>(category==="all"||r.kind===category)&&`${r.title} ${r.excerpt} ${r.topics.join(" ")}`.toLowerCase().includes(search.toLowerCase()));
  $("news-count").textContent=`已显示 ${Math.min(newsLimit,items.length)} / ${items.length} 条匹配报道 · 本次载入 ${report.news.items.length} 条 / 归档相关报道 ${report.news.count} 条`;
  $("news-list").innerHTML=items.slice(0,newsLimit).map(r=>`<article class="news-card"><div class="meta">${time(r.published_at)} · ${esc(r.kind)}</div><h3>${esc(r.title)}</h3><p>${esc(r.excerpt)}</p><div>${r.sources.map(s=>safeLink(s.url)?`<a href="${esc(safeLink(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.name)} ↗</a>`:esc(s.name)).join(" ")}</div>${r.risk_words.length?`<p>核对线索：${r.risk_words.map(esc).join(" · ")}</p>`:""}${r.mentioned_stocks.length?`<p>直接提及：${r.mentioned_stocks.map(s=>`${esc(s.name)} ${pct(s.pct)}`).join(" / ")}</p>`:""}</article>`).join("")||'<p class="muted">暂无匹配报道；缺少报道不代表没有风险。</p>';
  $("news-more").hidden=items.length<=newsLimit;
}
function render() {
  const r=data.report;
  if(r&&!archive){
    const parts=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date()).split(" ");
    const clock=parts[1], day=parts[0], anchor=clock>"15:05"?"15:05":clock>"11:30"&&clock<"13:00"?"11:30":clock;
    if(day.replaceAll("-","")!==r.day||new Date(`${day}T${anchor}:00+08:00`)-new Date(r.captured_at)>(data.interval_minutes+5)*60000){data={...data,status:"stale",message:"行情已过期，保留最近已保存的分析，请核对截止时间。"};}
  }
  const stale=data.status==="stale";
  $("sync-status").className=`notice${stale||data.refresh?.status==="failed"?" warning":""}`;
  $("sync-status").textContent=`${archive?"正在查看历史快照。 ":""}${data.message}${data.refresh?.status==="failed"?" 最近采集失败，保留此前结果。":""}${data.published_at?` 公开同步 ${time(data.published_at)}。`:""}`;
  const history=latestData.history||[];
  $("history").innerHTML='<option value="">跟随最新</option>'+history.map(h=>`<option value="${esc(h.window)}">${time(h.captured_at)}</option>`).join("");$("history").value=archive;
  if(!r){window.REALTIME_READY=true;return;}
  const m=r.market, ai=r.ai, s=r.sentiment;
  const references=(ids)=>ids.map(id=>`<a class="references" href="#evidence-${esc(id)}">[依据]</a>`).join("");
  $("content").innerHTML=`<p class="meta">行情 ${time(r.quote_start)}～${time(r.quote_end)} · 采集 ${time(r.captured_at)} · 新闻截至 ${time(r.news.as_of)}（北京时间）<br>报价 ${m.count}/${r.expected} 只 · 覆盖率 ${num(r.coverage,1)}% · 日线参考截至 ${esc(r.basis_day)}</p>
    <div class="metrics">${[["沪深300",pct(m.benchmark.pct),`最新 ${num(m.benchmark.price)} · ${time(m.benchmark.quote_time)}`,tone(m.benchmark.pct)],["上涨家数占比",`${num(m.up_ratio,1)}%`,`${m.up} 涨 / ${m.down} 跌 / ${m.flat} 平`,""],["累计成交额",`${num(m.amount/1e8,0)}<small>亿元</small>`,"已采集样本累计值，不推算全天", ""],["站上MA20",m.above_ma20_ratio==null?"—":`${num(m.above_ma20_ratio,1)}%`,`${m.ma20_count} 只有完整可比日线参考`,""]].map(([k,v,n,c])=>`<article><span>${k}</span><strong class="${c}">${v}</strong><small>${esc(n)}</small></article>`).join("")}</div>
    <section class="panel"><div class="section-head"><div><h2>AI盘中研判</h2><p class="meta">${esc(ai.message)}${ai.generated_at?`<br>生成 ${time(ai.generated_at)} · 分析快照 ${time(ai.as_of)} · ${esc(ai.model)}`:""}</p></div><span class="badge">${esc({available:"本次分析完成",pending:"本次分析生成中",failed:"本次AI分析未完成",disabled:"自动AI未启用",unconfigured:"AI待配置"}[ai.status]||"状态待核对")}</span></div>${ai.status==="available"?`<div class="ai-grid">${Object.entries({overview:"市场研判",focus:"关注方向",risks:"主要风险",watchpoints:"后续验证"}).map(([key,label])=>`<article class="ai-block"><h3>${label}</h3><ul>${ai.sections[key].map(i=>`<li>${esc(i.text)} ${references(i.evidence_ids)}</li>`).join("")}</ul></article>`).join("")}</div>`:'<p class="muted">以下行情与新闻仍可独立阅读；不会用旧AI结论冒充本次分析。</p>'}</section>
    <section class="panel"><div class="section-head"><div><h2>市场与板块</h2><p class="meta">行业等权样本，按平均涨跌排序；观察异动不等于买入信号。</p></div></div><div class="table-wrap"><table><thead><tr><th>行业</th><th>平均涨跌</th><th>上涨家数</th><th>累计成交额</th><th>样本</th></tr></thead><tbody>${r.industries.map(i=>`<tr><td>${esc(i.name)}<small>${i.leaders.map(l=>esc(l.name)).join(" / ")}</small></td><td class="${tone(i.pct)}">${pct(i.pct)}</td><td>${num(i.up_ratio,0)}%</td><td>${num(i.amount/1e8,1)} 亿</td><td>${i.count}</td></tr>`).join("")}</tbody></table></div></section>
    <section class="panel"><h2>新闻与舆情线索</h2><p class="meta">${esc(s.method)}</p><div class="badges">${[["报道样本",s.sample_count],["风险事项",s.risk_count],["传闻 / 澄清",s.unverified_count],["政策报道",s.policy_count]].map(([k,v])=>`<span class="badge">${k} ${v}</span>`).join("")}</div><div class="badges">${s.topics.map(t=>`<span class="badge">${esc(t.name)} · ${t.count}条提及</span>`).join("")||'<span class="muted">当前没有可确认的主题热度样本。</span>'}</div><p class="meta">${r.news.sources.map(i=>`${esc(i.name)}：${esc({available:"可用",empty:"本次为空",partial:"部分可用",unavailable:"接口暂不可用",stale:"采集已过期",not_collected:"尚未采集"}[i.status]||i.status)} ${time(i.fetched_at)}`).join("<br>")}</p>
    <div class="news-tools"><input id="news-search" type="search" placeholder="搜索主题、公司或新闻" aria-label="搜索新闻"><select id="news-kind" aria-label="新闻类型"><option value="all">全部报道</option><option>风险事项待核对</option><option>传闻/澄清待核对</option><option>政策/监管报道</option><option>行业/公司报道</option><option>行情报道</option></select></div><p id="news-count" class="meta"></p><div id="news-list" class="news-grid"></div><button id="news-more" type="button">查看更多报道</button></section>
    <section class="panel"><h2>量价观察</h2><p class="meta">个股报价均有独立时间；累计量/全天均量不代表同时间放量。</p><div class="two-col">${[["涨幅观察",r.leaders],["跌幅观察",r.laggards]].map(([label,rows])=>`<div><h3>${label}</h3><div class="table-wrap"><table><thead><tr><th>股票</th><th>涨跌</th><th>距MA20</th><th>累计量/全天均量</th></tr></thead><tbody>${rows.map(q=>`<tr><td>${esc(q.name)}<small>${esc(q.code)} · ${time(q.quote_time)}</small></td><td class="${tone(q.pct)}">${pct(q.pct)}</td><td>${pct(q.distance_ma20)}</td><td>${num(q.cumulative_daily_volume_ratio)} ×</td></tr>`).join("")}</tbody></table></div></div>`).join("")}</div></section>
    <section class="panel"><details><summary>数据依据与范围 · ${r.evidence.length}项</summary>${r.evidence.map(e=>`<article class="evidence-card" id="evidence-${esc(e.id)}"><h3>${esc(e.title)}</h3><pre>${esc(e.text)}</pre>${(e.sources||[]).map(s=>safeLink(s.url)?`<a href="${esc(safeLink(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.name)}</a>`:"").join(" ")}</article>`).join("")}<ul>${r.notes.map(n=>`<li class="meta">${esc(n)}</li>`).join("")}</ul></details></section>`;
  $("news-search").value=search;$("news-kind").value=category;
  $("news-search").addEventListener("input",e=>{search=e.target.value;newsLimit=24;newsCards(r);});
  $("news-kind").addEventListener("change",e=>{category=e.target.value;newsLimit=24;newsCards(r);});
  $("news-more").addEventListener("click",()=>{newsLimit+=24;newsCards(r);});
  newsCards(r);window.REALTIME_READY=true;
}
async function fetchData(url) {const r=await fetch(url,{cache:"no-store"});if(!r.ok)throw new Error("数据同步暂不可用");return r.json();}
async function refresh(manual=false) {
  if(loading||location.protocol==="file:")return;
  loading=true;$("refresh").disabled=true;
  try {
    const url=new URL(endpoint,location.href);url.searchParams.set("v",String(Date.now()));
    if(archive&&!url.hostname.endsWith("githubusercontent.com"))url.searchParams.set("window",archive);
    let fresh;
    try {
      fresh=await fetchData(url.href);
      const clock=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date());
      const trading=(clock>="09:30"&&clock<="11:30")||(clock>="13:00"&&clock<="15:05");
      const older=latestData.report&&(!fresh.report||Date.parse(fresh.report.captured_at)<Date.parse(latestData.report.captured_at));
      const delayed=trading&&fresh.report&&Date.now()-Date.parse(fresh.report.captured_at)>(data.interval_minutes+2)*60000;
      if(!archive&&url.hostname.endsWith("githubusercontent.com")&&(older||delayed))throw new Error("缓存仍是较旧快照");
    }catch(error){
      if(!url.hostname.endsWith("githubusercontent.com")||Date.now()-lastApiAttempt<180000)throw error;
      lastApiAttempt=Date.now();const parts=url.pathname.split("/");
      fresh=await fetchData(`https://api.github.com/repos/${parts[1]}/${parts[2]}/contents/realtime.json?ref=pages-live&v=${Date.now()}`).then(r=>JSON.parse(decodeURIComponent(escape(atob(r.content.replace(/\s/g,""))))));
    }
    if(fresh.version!=="realtime-analysis-v1"||fresh.source!==latestData.source||!Array.isArray(fresh.history))throw new Error("数据版本或来源待核验");
    if(fresh.report&&(!Number.isFinite(Date.parse(fresh.report.captured_at))||!fresh.report.ai||!Array.isArray(fresh.report.evidence)))throw new Error("分析快照字段待核验");
    if(!archive&&latestData.report&&(!fresh.report||Date.parse(fresh.report.captured_at)<Date.parse(latestData.report.captured_at)))throw new Error("上游返回较旧快照，等待下次同步");
    if(!archive&&latestData.report?.ai?.status==="available"&&fresh.report?.captured_at===latestData.report.captured_at&&fresh.report.ai.status==="pending")throw new Error("上游AI结果尚未同步完成");
    if(!archive) {latestData=fresh;data=fresh;render();}
    else if(!url.hostname.endsWith("githubusercontent.com")) {data=fresh;render();}
  } catch(error) {$("sync-status").textContent=`同步失败，保留当前结果。${error.message}；可点击重试。`;$("sync-status").className="notice warning";}
  finally {loading=false;$("refresh").disabled=false;}
}
$("refresh").addEventListener("click",()=>{archive="";refresh(true);});
$("history").addEventListener("change",async e=>{archive=e.target.value;if(!archive){data=latestData;render();refresh();return;}if(endpoint.startsWith("http")){const old=[latestData.report,...(latestData.archives||[])].filter(Boolean).find(r=>r.window===archive);if(old){data={...latestData,report:old,message:"历史分析按当时数据和AI结果保存。"};render();}else{$("sync-status").textContent="该历史时点尚未同步，请稍后重试。";}}else await refresh();});
render();refresh();setInterval(()=>{if(!document.hidden)refresh();},60000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});window.addEventListener("pageshow",()=>refresh());

document.addEventListener("click",e=>{const link=e.target.closest("a[href^=\"#evidence-\"]");if(link){const node=document.getElementById(link.hash.slice(1));const details=node?.closest("details");if(details)details.open=true;}});
