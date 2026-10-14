"use strict";
(() => {
  const hero = document.querySelector('[data-live-url]');
  const endpoint = hero?.dataset.liveUrl;
  if (!endpoint || !/^https?:$/.test(location.protocol)) return;
  let pending = false, lastApiAttempt = 0, lastDataAt = 0;
  const label = (value) => !value ? '暂无' : String(value).slice(0,19).replace('T',' ');
  const money = (value) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
  function expired(data) {
    const v = data?.backtest?.valuation;
    if (!v || !['available','stale'].includes(v.status)) return false;
    const parts = Object.fromEntries(new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(new Date()).map((p) => [p.type,p.value]));
    const today = `${parts.year}${parts.month}${parts.day}`, clock = `${parts.hour}:${parts.minute}`;
    const cutoff = clock > '15:00' ? '15:00' : clock > '11:30' && clock < '13:00' ? '11:30' : clock;
    return v.status === 'stale' || today !== v.day || Date.parse(`${parts.year}-${parts.month}-${parts.day}T${cutoff}:00+08:00`) - Date.parse(v.quote_start) > (v.interval_minutes + 5) * 60000;
  }
  async function read(url,backup = false) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),6000);
    try {
      url.searchParams.set('t',String(Date.now()));
      const response = await fetch(url,{cache:'no-store',credentials:'omit',signal:controller.signal,headers:backup ? {Accept:'application/vnd.github.raw+json'} : {}});
      if (!response.ok) throw new Error('同步不可用');
      const data = await response.json();
      if (data.version !== 'portfolio-live-v1' || !data.backtest?.summary || !Number.isFinite(Date.parse(data.data_at))) throw new Error('数据格式无效');
      return data;
    } finally {clearTimeout(timeout);}
  }
  async function refresh() {
    if (pending || document.hidden) return;
    pending = true;
    try {
      const url = new URL(endpoint);
      let data, backup = false;
      try {data = await read(new URL(url));} catch (_) {}
      if ((!data || expired(data) || Date.parse(data.data_at) < lastDataAt) && Date.now() - lastApiAttempt >= 120000) {
        const parts = url.pathname.split('/').filter(Boolean);
        if (url.hostname === 'raw.githubusercontent.com' && parts.length === 4) {
          lastApiAttempt = Date.now();
          const api = new URL(`https://api.github.com/repos/${parts[0]}/${parts[1]}/contents/${parts[3]}`);
          api.searchParams.set('ref',parts[2]);
          try {data = await read(api,true);backup = true;} catch (_) {}
        }
      }
      if (!data || Date.parse(data.data_at) < lastDataAt) throw new Error('暂未读到更新数据');
      lastDataAt = Date.parse(data.data_at);
      const valuation = data.backtest.valuation || {}, account = valuation.summary || data.backtest.summary;
      const day = valuation.day || data.report_day, stale = expired(data);
      const captured = Date.parse(data.data_at);
      const newer = captured >= Date.parse(hero.dataset.reportAt);
      if (newer) {
        document.getElementById('latest-title').textContent = '最新报告 · 模拟持仓 · 盘中估值';
        document.getElementById('latest-day').textContent = `数据日期 ${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6,8)}`;
        document.getElementById('latest-time').textContent = `行情采集 ${label(data.data_at)} · 北京时间`;
        document.getElementById('latest-link').href = `portfolio.html?v=${captured}`;
      }
      const card = document.getElementById('portfolio-time');
      if (card) card.textContent = `行情采集 ${label(data.data_at)} · 数据同步 ${label(data.published_at)}`;
      const status = document.getElementById('live-status');
      status.textContent = `持仓行情每${valuation.interval_minutes || 10}分钟采集；页面每分钟检查更新。同步 ${label(data.published_at)}${backup ? ' · 备用通道' : ''}。${stale ? '行情已过期，显示最后保存的估值。' : valuation.message || ''} 图片和下载文件为生成时快照。`;
      const element = document.getElementById('live-account');
      element.hidden = false;
      element.textContent = `初始资金 ${money(account.initial_capital)} 元 · 持仓市值 ${money(account.market_value)} 元 · 可用资金 ${money(account.available_cash)} 元 · 盈亏 ${money(account.profit)} 元`;
    } catch (_) {
      document.getElementById('live-status').textContent = '最新持仓同步暂不可用，保留当前数据；请查看行情采集时间。';
    } finally { pending = false; }
  }
  refresh();
  setInterval(refresh,60000);
  document.addEventListener('visibilitychange',refresh);
  window.addEventListener('pageshow',refresh);
  window.addEventListener('focus',refresh);
})();
