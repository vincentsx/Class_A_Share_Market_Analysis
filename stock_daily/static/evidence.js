/* Shared evidence presentation for live pages and standalone reports. */
(() => {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const number = (v, d=2) => typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("zh-CN", {minimumFractionDigits:d,maximumFractionDigits:d}) : "—";
  const percent = v => `${v > 0 ? "+" : ""}${number(v)}%`;
  const stamp = v => String(v || "").slice(0,19).replace("T"," ");
  const url = v => {try {const u=new URL(v);return /^https?:$/.test(u.protocol)&&!u.username&&!u.password?u.href:"";} catch (_) {return "";}};
  let sequence = 0;
  function describe(cards=[]) {
    return cards.map((card,index) => {
      const item={id:card.id,number:index+1,title:card.title||"报告依据",kind:"报告资料",facts:[],groups:[],text:"",time:card.published_at,sources:card.sources||[]};
      let data;try {data=JSON.parse(card.text);} catch (_) {data=null;}
      const fact=(label,value,formatter=number)=>{if(typeof value==="number"&&Number.isFinite(value))item.facts.push({label,value:formatter(value)});};
      const rows=(title,entries,format)=>{if(entries?.length)item.groups.push({title,rows:entries.map(format)});};
      const industry=r=>{
        const parts=[];
        for(const [key,label,format] of [["pct","平均涨跌",percent],["relative20","近20日相对强度",v=>`${number(v)} 个百分点`],["up_ratio","上涨占比",v=>`${number(v,1)}%`],["count","样本",v=>`${number(v,0)}只`],["amount","成交额",v=>`${number(v/1e8)}亿元`]]) {
          if(r[key]!=null)parts.push(`${label} ${format(r[key])}`);
        }
        if(r.status)parts.push(r.status);
        return {title:r.name||"行业样本",text:parts.join(" · ")};
      };
      if(data&&typeof data==="object"&&!Array.isArray(data)) {
        if(card.id==="market") {
          item.kind="行情数据";const m=data.market||data,b=m.benchmark||{};
          fact("沪深300",b.price,v=>`${number(v)} 点`);fact("指数涨跌",b.pct??m.benchmark_pct,percent);
          for(const [key,label] of [["count","报价样本"],["up","上涨"],["down","下跌"],["flat","平盘"]])fact(label,m[key],v=>`${number(v,0)} 只`);
          fact("累计成交额",m.amount,v=>`${number(v/1e8)} 亿元`);fact("数据覆盖率",data.coverage,v=>`${number(v)}%`);
          item.time=data.as_of||b.quote_time;item.text="成交额为所标时点的累计值；盘中数据不代表全天结果。";
        } else if(card.id==="observations"||data.industries) {
          item.kind="行业与量价";rows("行业观察",data.industries,industry);
          rows("候选观察",data.candidates,r=>({title:`${r.name||r.code} · ${r.code}`,text:r.reasons?.join("；")||"报告中已留存的候选观察"}));
          rows("量价要点",data.analysis?.summaries,text=>({title:"",text}));
        } else if(card.id.startsWith("sector-")) {
          item.kind="行业数据";rows("行业观察",[data],industry);
        } else if(card.id.startsWith("stock-")) {
          item.kind="个股数据";const q=data.quote||{};
          fact("最新价",q.price,v=>`${number(v)} 元`);fact("开盘价",q.open,v=>`${number(v)} 元`);fact("涨跌幅",q.pct,percent);
          fact("累计成交额",q.amount,v=>`${number(v/1e8)} 亿元`);fact("模拟持仓",data.shares,v=>`${number(v,0)} 股`);item.time=q.quote_time;
          rows("观察条件",data.signal?.reasons,text=>({title:"",text}));rows("风险线索",data.signal?.risks,text=>({title:"",text}));
          item.text=data.role==="holding"?"已有模拟持仓的报价依据。":"已发布候选的报价与信号依据。";
        }
        if(!item.facts.length&&!item.groups.length&&!item.text)item.text="该项暂无可读摘要，请核对原报告中的对应数据。";
      } else {
        item.kind=card.id.startsWith("news-")?"新闻报道":"报告资料";
        item.text=data==null?(card.text||""):"请核对原报告中的对应数据。";
      }
      return item;
    });
  }
  function references(ids,cards,prefix) {
    const items=cards.map((card,index)=>({id:card.id,title:card.title||"报告依据",number:index+1}));
    return `<span class="source-citations" aria-label="分析依据">${ids.map(id=>{
      const item=items.find(e=>e.id===id);
      return item?`<a class="evidence-ref references" data-evidence-link href="#${esc(prefix)}-source-${item.number}" title="${esc(item.title)}">[${item.number}] ${esc(item.title)}</a>`:'<span class="muted">引用资料待核对</span>';
    }).join("")}</span>`;
  }
  function render(cards,prefix=`trade-source-${++sequence}`) {
    return describe(cards).map(c=>`<article class="analysis-evidence" id="${esc(prefix)}-source-${c.number}" tabindex="-1"><div class="evidence-heading"><span class="evidence-number">${c.number}</span><div><span class="evidence-kind">${esc(c.kind)}</span><h4>${esc(c.title)}</h4></div></div>${c.time?`<p class="evidence-time">资料时点 ${esc(stamp(c.time))} · 北京时间</p>`:""}${c.facts.length?`<dl class="evidence-facts">${c.facts.map(f=>`<div><dt>${esc(f.label)}</dt><dd>${esc(f.value)}</dd></div>`).join("")}</dl>`:""}${c.text?`<p class="evidence-summary">${esc(c.text)}</p>`:""}${c.groups.map(g=>`<div class="evidence-group"><h5>${esc(g.title)}</h5>${g.rows.map(r=>`<div class="evidence-row">${r.title?`<b>${esc(r.title)}</b>`:""}<p>${esc(r.text)}</p></div>`).join("")}</div>`).join("")}<div class="evidence-sources">${c.sources.map(s=>url(s.url)?`<a href="${esc(url(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.name||"查看原文")} ↗</a>`:`<span>${esc(s.name||"")}</span>`).join(" ")}</div></article>`).join("");
  }
  document.addEventListener("click",event=>{
    const link=event.target.closest("a[data-evidence-link]");if(!link)return;
    const target=document.getElementById(link.getAttribute("href").slice(1));if(!target)return;
    for(let parent=target.parentElement;parent;parent=parent.parentElement)if(parent.tagName==="DETAILS")parent.open=true;
    target.focus({preventScroll:true});
  });
  const remember = root => new Set([...root.querySelectorAll('.analysis-sources[open]')].map(e=>e.dataset.evidenceKey).filter(Boolean));
  const restore = (root,keys) => root.querySelectorAll('.analysis-sources[data-evidence-key]').forEach(e=>{
    if(keys.has(e.dataset.evidenceKey))e.open=true;
  });
  window.GuanshiEvidence={describe,references,render,remember,restore};
})();
