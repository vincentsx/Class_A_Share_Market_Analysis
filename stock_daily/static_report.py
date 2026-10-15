"""Readable report data for attachment viewers that do not execute scripts."""

import json
import math
from pathlib import Path
from urllib.parse import urlparse

NEWS_LABELS = {
    "available": "快讯已采集", "partial": "部分来源可用", "unavailable": "采集不可用 / 已过期",
    "not_collected": "该时点尚未采集", "disabled": "新闻未启用",
}
SOURCE_LABELS = {
    "available": "接口正常", "empty": "返回为空", "partial": "部分字段无效",
    "unavailable": "接口不可用", "stale": "采集记录较早", "not_collected": "尚未采集",
}
ANNOUNCEMENT_LABELS = {
    "available": "已采集，仅核对标题", "demo": "合成示例公告", "disabled": "未启用，风险未核验",
    "unavailable": "接口不可用，风险未核验", "not_requested": "未核验",
}


def number(value, digits=2):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:,.{digits}f}"


def percent(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return ("+" if value > 0 else "") + number(value) + "%"


def color(value):
    return "mail-up" if isinstance(value, (int, float)) and value > 0 else (
        "mail-down" if isinstance(value, (int, float)) and value < 0 else "")


def safe_link(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlparse(value)
        return value if parsed.scheme in {"https", "http"} and parsed.hostname and not (
            parsed.username or parsed.password) else ""
    except ValueError:
        return ""


def time_label(value):
    return str(value or "—")[:19].replace("T", " ")


def template_helpers(environment):
    environment.globals.update(mail_num=number, mail_pct=percent, mail_color=color,
                               mail_link=safe_link, mail_time=time_label,
                               evidence_cards=evidence_cards,
                               evidence_javascript=(Path(__file__).parent / 'static/evidence.js').read_text(),
                               mail_source_status=lambda s: SOURCE_LABELS.get(s, "状态待核对"),
                               mail_announcement_status=lambda s: ANNOUNCEMENT_LABELS.get(s, "状态待核对"))


def evidence_cards(cards):
    """Presentation only: keep source records and model citations unchanged."""
    result = []
    for index, card in enumerate(cards):
        item = {'id': card['id'], 'number': index + 1, 'title': card.get('title') or '报告依据',
                'kind': '报告资料', 'facts': [], 'groups': [], 'text': '',
                'time': card.get('published_at'), 'sources': card.get('sources', [])}
        try:
            data = json.loads(card.get('text', ''))
        except (ValueError, TypeError):
            data = None
        def fact(label, value, formatter=number):
            if isinstance(value, (int, float)) and math.isfinite(value):
                item['facts'].append({'label': label, 'value': formatter(value)})
        def rows(title, entries, describe):
            if entries:
                item['groups'].append({'title': title, 'rows': [describe(e) for e in entries]})
        def industry(row):
            parts = []
            for key, label, formatter in [('pct', '平均涨跌', percent), ('relative20', '近20日相对强度',
                    lambda v: number(v) + ' 个百分点'), ('up_ratio', '上涨占比', lambda v: number(v, 1) + '%'),
                    ('count', '样本', lambda v: number(v, 0) + '只'),
                    ('amount', '成交额', lambda v: number(v / 1e8) + '亿元')]:
                if row.get(key) is not None:
                    parts.append(label + ' ' + formatter(row[key]))
            if row.get('status'):
                parts.append(row['status'])
            return {'title': row.get('name', '行业样本'), 'text': ' · '.join(parts)}
        if isinstance(data, dict):
            if card['id'] == 'market':
                item['kind'] = '行情数据'
                market = data.get('market', data)
                benchmark = market.get('benchmark') or {}
                fact('沪深300', benchmark.get('price'), lambda v: number(v) + ' 点')
                fact('指数涨跌', benchmark.get('pct', market.get('benchmark_pct')), percent)
                for key, label in [('count', '报价样本'), ('up', '上涨'), ('down', '下跌'), ('flat', '平盘')]:
                    fact(label, market.get(key), lambda v: number(v, 0) + ' 只')
                fact('累计成交额', market.get('amount'), lambda v: number(v / 1e8) + ' 亿元')
                fact('数据覆盖率', data.get('coverage'), lambda v: number(v) + '%')
                item['time'] = data.get('as_of') or benchmark.get('quote_time')
                item['text'] = '成交额为所标时点的累计值；盘中数据不代表全天结果。'
            elif card['id'] == 'observations' or 'industries' in data:
                item['kind'] = '行业与量价'
                rows('行业观察', data.get('industries', []), industry)
                rows('候选观察', data.get('candidates', []), lambda r: {'title': (r.get('name') or r['code']) + ' · ' + r['code'],
                    'text': '；'.join(r.get('reasons') or []) or '报告中已留存的候选观察'})
                rows('量价要点', (data.get('analysis') or {}).get('summaries', []),
                    lambda text: {'title': '', 'text': text})
            elif card['id'].startswith('sector-'):
                item['kind'] = '行业数据'
                rows('行业观察', [data], industry)
            elif card['id'].startswith('stock-'):
                item['kind'] = '个股数据'
                quote = data.get('quote') or {}
                fact('最新价', quote.get('price'), lambda v: number(v) + ' 元')
                fact('开盘价', quote.get('open'), lambda v: number(v) + ' 元')
                fact('涨跌幅', quote.get('pct'), percent)
                fact('累计成交额', quote.get('amount'), lambda v: number(v / 1e8) + ' 亿元')
                fact('模拟持仓', data.get('shares'), lambda v: number(v, 0) + ' 股')
                item['time'] = quote.get('quote_time')
                signal = data.get('signal') or {}
                rows('观察条件', signal.get('reasons') or [], lambda text: {'title': '', 'text': text})
                rows('风险线索', signal.get('risks') or [], lambda text: {'title': '', 'text': text})
                item['text'] = '已有模拟持仓的报价依据。' if data.get('role') == 'holding' else '已发布候选的报价与信号依据。'
            if not item['facts'] and not item['groups'] and not item['text']:
                item['text'] = '该项暂无可读摘要，请核对原报告中的对应数据。'
        else:
            item['kind'] = '新闻报道' if card['id'].startswith('news-') else '报告资料'
            item['text'] = card.get('text', '') if data is None else '请核对原报告中的对应数据。'
        result.append(item)
    return result


def snapshot_view(payload):
    analysis = payload.get("analysis", {})
    news = payload.get("news", {})
    market = payload["market"]
    count = market["count"]
    benchmark = market["benchmark"]
    return {
        "kind": "snapshot", "day": payload["day"], "slot": payload["slot"], "data": payload,
        "analysis": analysis, "news": news, "news_label": NEWS_LABELS.get(news.get("status"), "该时点尚未采集"),
        "metrics": [
            {"label": "沪深300", "value": percent(benchmark["pct"]), "color": color(benchmark["pct"]),
             "note": "最新 " + number(benchmark["price"])},
            {"label": "上涨股票占比", "value": number(market["up"] / count * 100 if count else None, 1) + "%",
             "note": f"{market['up']} 上涨 / {market['down']} 下跌 / {market['flat']} 平盘"},
            {"label": "上午累计成交额" if payload["slot"] == "midday" else "当日累计成交额",
             "value": number(market["amount"] / 1e8, 0) + " 亿", "note": "有效报价样本 · 未外推全天成交额"},
            {"label": "量能可比样本", "value": number(analysis.get("volume_coverage", 0), 0) + " 只",
             "note": f"报价 {count} 只 · 覆盖 {number(payload['coverage'])}%"},
        ],
        "groups": [
            {"key": "volume_up", "label": "放量观察", "total": analysis.get("counts", {}).get("volume_up", 0),
             "rows": analysis.get("volume_up", [])},
            {"key": "volume_down", "label": "缩量观察", "total": analysis.get("counts", {}).get("volume_down", 0),
             "rows": analysis.get("volume_down", [])},
            {"key": "risk_alerts", "label": "风险异动", "total": analysis.get("counts", {}).get("risk", 0),
             "rows": analysis.get("risk_alerts", [])},
        ],
    }


def daily_view(payload, *, fill_history=None):
    dashboard = payload["dashboard"]
    report = dashboard["report"]
    if fill_history is None:
        from .fills import history_fills

        fill_history = history_fills(payload.get("backtest"), dashboard.get("source", ""))
    result = {"kind": "daily", "day": report["day"] if report else None,
              "dashboard": dashboard, "report": report, "tracking": payload["tracking"], "metrics": [],
              "backtest": payload.get("backtest"),
              "fills": fill_history, "report_analysis": dashboard.get("report_analysis")}
    if not report:
        return result
    market = report["market"]
    result["metrics"] = [
        {"label": "沪深300", "value": percent(market["benchmark_pct"]), "color": color(market["benchmark_pct"]),
         "note": "近20日 " + percent(market["benchmark_return20"])},
        {"label": "上涨股票占比", "value": number(market["up_ratio"], 1) + "%",
         "note": f"{market['up']} 上涨 / {market['down']} 下跌 / {market['flat']} 平盘"},
        {"label": "全市场成交额", "value": number(market["amount"] / 1e8, 0) + " 亿",
         "note": "此前20日均值的 " + number(market["amount_ratio20"]) + " 倍"},
        {"label": "站上20日均线", "value": number(market["above_ma20_ratio"], 1) + "%",
         "note": f"{market['eligible_history_count']} 只连续历史样本"},
    ]
    return result
