"""Readable report data for attachment viewers that do not execute scripts."""

import math
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
                               mail_source_status=lambda s: SOURCE_LABELS.get(s, "状态待核对"),
                               mail_announcement_status=lambda s: ANNOUNCEMENT_LABELS.get(s, "状态待核对"))


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
