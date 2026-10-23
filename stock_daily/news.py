"""Archive public rolling news and relate reports to observable market data."""

import hashlib
import html
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import SHANGHAI, now
from .db import BusyError, news_fetches, news_items, sessions, snapshots
from .providers import DataError, parse_day

logger = logging.getLogger(__name__)
PROVIDERS = {"em": "东方财富快讯", "cls": "财联社电报"}
# These mappings identify a broad industry observation scope, never concept constituents.
TOPICS = {
    "宏观与政策": (("央行", "降准", "降息", "财政政策", "货币政策", "国务院", "证监会", "美联储"), ()),
    "人工智能": (("人工智能", "AI", "大模型", "算力", "光模块", "数据中心", "智能体"), ("I64", "I65", "C39", "软件", "半导体")),
    "半导体": (("半导体", "芯片", "晶圆", "光刻", "存储器", "存储芯片"), ("C39", "半导体")),
    "机器人": (("机器人", "人形", "工业自动化", "减速器", "伺服"), ("C34", "C35", "C39", "机械设备")),
    "新能源车": (("新能源汽车", "新能源车", "电动汽车", "汽车出口", "乘联", "智能驾驶"), ("C36", "汽车")),
    "电池与储能": (("固态电池", "锂电", "动力电池", "储能", "钠离子", "锂矿"), ("C38", "C39", "电力设备")),
    "光伏与风电": (("光伏", "风电", "风力发电", "太阳能", "硅料"), ("C38", "D44", "电力设备")),
    "电网与电力": (("电网", "特高压", "电力市场", "核电", "电价"), ("C38", "D44", "电力设备")),
    "低空与航天": (("低空", "无人机", "商业航天", "卫星", "航空航天"), ("C37", "航空")),
    "医药与健康": (("创新药", "医药", "医保", "医疗器械", "临床试验", "药品"), ("C27", "Q83", "医药")),
    "传媒与影视": (("影视", "电影", "票房", "短剧", "出版", "传媒", "游戏"), ("R85", "R86", "R87", "I65", "传媒")),
    "银行与金融": (("银行", "券商", "证券公司", "保险", "公募基金", "资本市场"), ("J66", "J67", "J68", "J69", "银行")),
    "地产与基建": (("房地产", "地产", "住房", "楼市", "基建", "基础设施", "城市更新"), ("K70", "E47", "E48", "E49", "房地产")),
    "消费与零售": (("消费", "零售", "白酒", "免税", "电商", "家电", "旅游"), ("C15", "F51", "F52", "H61", "消费")),
    "有色与贵金属": (("有色", "黄金", "金价", "贵金属", "铜价", "稀土", "铝价"), ("B09", "B10", "C32", "有色")),
    "能源与地缘": (("原油", "油价", "天然气", "石油", "煤炭", "霍尔木兹", "中东", "制裁", "关税"), ("B06", "B07", "C25", "能源")),
}
RISK_WORDS = ("减持", "立案", "诉讼", "处罚", "退市", "预亏", "亏损", "违约", "风险警示", "事故", "制裁")
UNVERIFIED_WORDS = ("传闻", "网传", "据传", "市场消息", "消息人士", "尚未证实", "未经证实", "澄清", "辟谣", "否认")
RISK_ATTRIBUTION_VERSION = "stock-risk-v1"


def plain(value):
    value = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", value)).strip()


def local_time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, SHANGHAI)
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return timestamp.replace(tzinfo=SHANGHAI) if timestamp.tzinfo is None else timestamp.astimezone(SHANGHAI)


def fingerprint(title, published_at):
    normalized = re.sub(r"[\W_]+", "", title).casefold()
    return hashlib.sha256((published_at[:10] + normalized).encode()).hexdigest()


def normalize(provider, raw, captured):
    content = plain(raw.get("content", raw.get("summary", "")))[:2000]
    title = plain(raw.get("title"))
    if not title:
        heading = re.match(r"[【\[]([^】\]]+)[】\]]", content)
        title = heading[1] if heading else content[:80]
    if not title:
        raise ValueError("新闻标题与内容均为空")
    published = local_time(raw["ctime"] if provider == "cls" else raw["showTime"])
    if published > captured + timedelta(seconds=5):
        raise ValueError("新闻发布时间在未来")
    identifier = str(raw.get("id" if provider == "cls" else "code", ""))
    url = (f"https://www.cls.cn/detail/{identifier}" if provider == "cls" else
           f"https://finance.eastmoney.com/a/{identifier}.html") if identifier.isdigit() else (
               "https://www.cls.cn/telegraph" if provider == "cls" else "https://kuaixun.eastmoney.com/7_24.html")
    title = title[:300]
    version = hashlib.sha256((published.isoformat() + title + content).encode()).hexdigest()
    return {"provider": provider, "id": version, "published_at": published.isoformat(),
            "first_seen_at": captured.isoformat(), "title": title, "content": content,
            "url": url, "important": int(raw.get("level") in {"A", "B"})}


class PublicNewsFeed:
    def __init__(self, timeout):
        self.timeout = timeout

    def fetch(self, provider, captured):
        if provider == "em":
            url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
            params = {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "",
                      "pageSize": "200", "req_trace": str(int(captured.timestamp() * 1000))}
            referer = "https://kuaixun.eastmoney.com/"
        else:
            url = "https://www.cls.cn/v1/roll/get_roll_list"
            params = {"app": "CailianpressWeb", "category": "", "last_time": int(captured.timestamp()),
                      "os": "web", "refresh_type": "1", "rn": "20", "sv": "8.4.6"}
            params["sign"] = hashlib.md5(hashlib.sha1(urlencode(params).encode()).hexdigest().encode()).hexdigest()
            referer = "https://www.cls.cn/"
        started = time.monotonic()
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0", "Referer": referer}) as client:
            with client.stream("GET", url, params=params) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000 or time.monotonic() - started > self.timeout:
                        raise DataError("新闻响应超过大小或总时限")
        import json

        data = json.loads(body)["data"]
        rows = data["fastNewsList" if provider == "em" else "roll_data"]
        if not isinstance(rows, list):
            raise DataError("新闻响应字段无效")
        return rows


def collect_news(db, settings, force=False, feed=None, captured=None):
    if not settings.news_enabled or settings.source == "demo":
        return {"status": "disabled"}
    captured = captured or now()
    requested = []
    for provider in PROVIDERS:
        last = db.rows(select(news_fetches.c.fetched_at).where(news_fetches.c.provider == provider)
                       .order_by(news_fetches.c.fetched_at.desc()).limit(1))
        if force or not last or captured - local_time(last[0]["fetched_at"]) >= timedelta(
                minutes=settings.news_interval_minutes):
            requested.append(provider)
    if not requested:
        return {"status": "cached"}
    feed = feed or PublicNewsFeed(settings.news_timeout)

    def fetch(provider):
        try:
            raw = feed.fetch(provider, captured)
            valid, invalid = {}, 0
            for item in raw:
                try:
                    row = normalize(provider, item, captured)
                    valid[row["id"]] = row
                except (ValueError, TypeError, KeyError, OverflowError, OSError):
                    invalid += 1
            status = "available" if valid else "unavailable" if invalid else "empty"
            return provider, list(valid.values()), invalid, status, "字段或时间无效" if invalid else ""
        except Exception as error:
            logger.warning("%s 新闻采集失败：%s", provider, type(error).__name__)
            return provider, [], 0, "unavailable", f"接口不可用（{type(error).__name__}）"

    try:
        with db.lock("news"):
            with ThreadPoolExecutor(max_workers=len(requested)) as pool:
                results = list(pool.map(fetch, requested))
            constructor = sqlite_insert if db.engine.dialect.name == "sqlite" else pg_insert
            with db.engine.begin() as connection:
                for provider, rows, invalid, status, error in results:
                    # The first observation of a news version is immutable.
                    for row in rows:
                        connection.execute(constructor(news_items).values(row).on_conflict_do_nothing())
                    db.upsert(connection, news_fetches, {
                        "provider": provider, "fetched_at": captured.isoformat(), "status": status,
                        "count": len(rows), "invalid_count": invalid, "error": error,
                    })
            return {"status": "success" if all(r[3] in {"available", "empty"} for r in results) else "partial",
                    "providers": {r[0]: {"status": r[3], "count": len(r[1]), "invalid_count": r[2]} for r in results}}
    except BusyError:
        return {"status": "busy"}


def topic_matches(text):
    return [name for name, (words, _) in TOPICS.items() if any(
        re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", text, re.I) if word == "AI" else word in text
        for word in words)]


def kind_of(text):
    if any(word in text for word in UNVERIFIED_WORDS):
        return "传闻/澄清待核对"
    if any(word in text for word in RISK_WORDS):
        return "风险事项待核对"
    if any(word in text for word in ("收评", "午评", "盘面", "涨停", "板块走强", "板块下挫", "主力资金",
                                     "板块再度", "涨超", "跌超", "资金净流")):
        return "行情报道"
    if any(word in text for word in ("国务院", "央行", "证监会", "政策", "监管", "办法", "意见", "商务部")):
        return "政策/监管报道"
    return "行业/公司报道"


def name_index(rows):
    names = defaultdict(list)
    for row in rows:
        if len(row["name"]) >= 3:
            names[row["name"][:2]].append((row["name"], row["code"]))
    return names


def mentioned_codes(text, rows, names):
    codes = {code for token in re.findall(r"(?<!\d)\d{6}(?!\d)", text)
             for code in (token + ".SH", token + ".SZ") if code in rows}
    for index in range(len(text) - 1):
        for name, code in names.get(text[index:index + 2], ()):
            if text.startswith(name, index):
                codes.add(code)
    return codes


def risk_subjects(clause, direct, rows):
    """Bind each keyword to its nearest named subject or explicit subject list."""
    spans = sorted({(m.start(), m.end(), code) for code in direct
                    for token in (rows[code]['name'], code[:6])
                    for m in re.finditer(re.escape(token), clause)})
    targets = set()
    for risk in re.finditer('|'.join(map(re.escape, RISK_WORDS + UNVERIFIED_WORDS)), clause):
        preceding = [s for s in spans if s[1] <= risk.start()]
        following = [s for s in spans if s[0] >= risk.end()]
        group = preceding[-1:] if preceding else following[:1]
        if preceding:
            for earlier in reversed(preceding[:-1]):
                gap = clause[earlier[1]:group[0][0]]
                if not re.fullmatch(r"\s*(?:、|及|和|与|以及|及其)\s*", gap):
                    break
                group.insert(0, earlier)
        targets.update(s[2] for s in group)
    return targets


def stock_risks(title, content, rows, names):
    """Attribute a clue only to named subjects in that clause, retaining its text.

    Article-wide keywords remain visible, but do not implicate every stock in a
    roundup. This is a text association, not verification that a risk is true.
    """
    text = title + "。" + content.replace("【" + title + "】", "")
    title_codes = mentioned_codes(title, rows, names)
    all_codes = mentioned_codes(text, rows, names)
    single_subject = title_codes if len(title_codes) == 1 and all_codes == title_codes else set()
    result = []
    seen = set()
    for sentence in re.split(r"[。！？!?；;\n]+", text):
        subjects = set()
        for clause in re.split(r"[，,]+", sentence):
            direct = mentioned_codes(clause, rows, names)
            if direct:
                subjects = direct
            words = [word for word in RISK_WORDS + UNVERIFIED_WORDS if word in clause]
            if not words:
                continue
            targets = risk_subjects(clause, direct, rows)
            # A pronoun/announcement continuation can refer to a single known
            # subject. Never carry a list of stocks into an unrelated clause.
            if not targets and re.match(r"\s*(?:该公司|公司|本公司|其|控股股东|股东|预计)", clause):
                targets = subjects if len(subjects) == 1 else single_subject
            for code in sorted(targets):
                key = (code, clause.strip())
                if key in seen:
                    continue
                seen.add(key)
                result.append({"code": code, "name": rows[code]['name'], "kind": kind_of(clause),
                               "text": clause.strip()[:300], "risk_words": words})
    return result


def risk_stock_codes(item):
    """Old frozen snapshots retain their original conservative risk gate."""
    if item.get('risk_attribution_version') == RISK_ATTRIBUTION_VERSION:
        clues = item.get('stock_risks')
        if isinstance(clues, list) and all(isinstance(c, dict) and c.get('code') and c.get('text') for c in clues):
            return {c['code'] for c in clues}
    return {s['code'] for s in item.get('mentioned_stocks', [])}


def related_industries(topics, industries):
    scopes = [prefix for topic in topics for prefix in TOPICS[topic][1]]
    return [s for s in industries if any(s["name"].startswith(p) or p in s["name"] for p in scopes)]


def safe_url(value):
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname and (
        parsed.hostname == "cls.cn" or parsed.hostname.endswith(".cls.cn") or
        parsed.hostname == "eastmoney.com" or parsed.hostname.endswith(".eastmoney.com")) else ""


def build_news(db, settings, payload, as_of=None):
    cutoff = local_time(as_of or payload["captured_at"])
    quote_cutoff = local_time(payload["captured_at"])
    result = {"status": "disabled", "as_of": cutoff.isoformat(), "quote_cutoff": quote_cutoff.isoformat(),
              "supplemental": cutoff > quote_cutoff + timedelta(seconds=5), "items": [], "topics": [],
              "sources": [], "count": 0, "new_count": None, "window_start": (cutoff - timedelta(hours=24)).isoformat(),
              "notes": ["滚动快讯仅覆盖已采集内容，不保证全天完整；同日同标题归并，不同措辞可能仍指向同一事件。",
                        "新闻热度为本地收录提及数；收录渠道数量不表示独立核实。关键词映射只提供行业观察范围，不是概念成分股。",
                        "相关行业和提及个股只展示行情快照的量价事实，未验证新闻驱动关系；行情报道本身不当作独立催化事件。",
                        "保留来源和发布时间；传闻、澄清、风险事项单独标记，正文和事件影响方向仍需核对。"]}
    if not settings.news_enabled or settings.source == "demo":
        return result
    for provider, name in PROVIDERS.items():
        saved = db.rows(select(news_fetches).where(news_fetches.c.provider == provider,
                                                  news_fetches.c.fetched_at <= cutoff.isoformat())
                        .order_by(news_fetches.c.fetched_at.desc()).limit(1))
        entry = saved[0] if saved else {"status": "not_collected", "fetched_at": "", "count": 0,
                                       "invalid_count": 0, "error": "截至该时点没有采集记录"}
        entry["name"] = name
        if entry["fetched_at"] and cutoff - local_time(entry["fetched_at"]) > timedelta(
                minutes=max(30, settings.news_interval_minutes * 2)):
            entry["status"] = "stale"
        result["sources"].append(entry)
    statuses = [r["status"] for r in result["sources"]]
    result["status"] = ("available" if all(s in {"available", "empty"} for s in statuses) else
                        "partial" if any(s in {"available", "empty"} for s in statuses) else
                        "not_collected" if all(s == "not_collected" for s in statuses) else "unavailable")
    records = db.rows(select(news_items).where(news_items.c.published_at >= result["window_start"],
                                               news_items.c.published_at <= cutoff.isoformat(),
                                               news_items.c.first_seen_at <= cutoff.isoformat())
                      .order_by(news_items.c.published_at.desc(), news_items.c.first_seen_at.desc()).limit(5000))
    grouped = {}
    for record in records:
        key = fingerprint(record["title"], record["published_at"])
        link = {"name": PROVIDERS[record["provider"]], "url": safe_url(record["url"]),
                "published_at": record["published_at"]}
        if key in grouped:
            group = grouped[key]
            if any(s["name"] == PROVIDERS[record["provider"]] for s in group["sources"]):
                # A later observed version from one channel supersedes that
                # channel's older text at this cutoff, without rewriting history.
                continue
            if link not in group["sources"]:
                group["sources"].append(link)
            group["published_at"] = min(group["published_at"], record["published_at"])
            group["important"] = max(group["important"], record["important"])
            if record["content"] not in group["content"]:
                group["content"] = (group["content"] + " " + record["content"])[:4000]
            continue
        grouped[key] = {**record, "id": key, "sources": [link]}
    rows = {r["code"]: r for r in payload["quotes"]}
    names = name_index(payload["quotes"])
    watched = {r["code"] for r in payload.get("focus", [])}
    industries = payload.get("industries", [])
    morning = db.rows(select(snapshots.c.payload).where(
        snapshots.c.source == settings.source, snapshots.c.day == payload["day"], snapshots.c.slot == "midday"))
    old_news = morning[0]["payload"].get("news") if morning and payload["slot"] == "close" else None
    old_ids = set(old_news.get("tracked_ids", [])) if old_news else set()
    can_compare = bool(old_news and old_news["status"] in {"available", "partial"}
                       and not old_news.get("supplemental") and local_time(old_news["as_of"]) <= cutoff)
    items = []
    for item in grouped.values():
        text = item["title"] + " " + item["content"]
        topics = topic_matches(text)
        codes = mentioned_codes(text, rows, names)
        references = related_industries(topics, industries)
        direct_industries = {rows[c]["industry"] for c in codes}
        references = [s for s in industries if s in references or s["name"] in direct_industries]
        if not topics and not codes and not item["important"]:
            continue
        kind = kind_of(text)
        clues = stock_risks(item['title'], item['content'], rows, names)
        item.update(topics=topics, kind=kind, excerpt=item.pop("content")[:160],
                    risk_attribution_version=RISK_ATTRIBUTION_VERSION, stock_risks=clues,
                    risk_words=[word for word in RISK_WORDS + UNVERIFIED_WORDS if word in text],
                    related_sectors=[{"name": s["name"], "pct": s["pct"], "up_ratio": s["up_ratio"],
                                      "active": s.get("active", False),
                                      "basis": "直接提及个股所属行业" if s["name"] in direct_industries else "主题关键词映射"}
                                     for s in references],
                    mentioned_stocks=[{"code": c, "name": rows[c]["name"], "pct": rows[c]["pct"],
                                       "volume_ratio": rows[c].get("volume_ratio"), "watched": c in watched}
                                      for c in sorted(codes)],
                    after_quote=local_time(item["published_at"]) > quote_cutoff,
                    new_since_midday=item["id"] not in old_ids if can_compare else None)
        # Prioritize user interests and observable connections, not a made-up impact score.
        event = kind != "行情报道"
        item["priority"] = (event and bool(codes & watched), event, bool(item["risk_words"]),
                            kind == "政策/监管报道", bool(codes & watched),
                            any(s.get("active") for s in references), item["important"], bool(codes))
        items.append(item)
    items.sort(key=lambda r: (r["priority"], r["published_at"]), reverse=True)
    themes = []
    for name in TOPICS:
        matching = [r for r in items if name in r["topics"]]
        if not matching:
            continue
        references = related_industries([name], industries)
        direct = {r["code"]: r for item in matching for r in item["mentioned_stocks"]}
        themes.append({"name": name, "count": len(matching), "latest_at": max(r["published_at"] for r in matching),
                       "new_count": sum(r["new_since_midday"] is True for r in matching) if can_compare else None,
                       "related_sectors": [{"name": s["name"], "pct": s["pct"], "up_ratio": s["up_ratio"],
                                            "active": s.get("active", False)} for s in references],
                       "mentioned_stocks": list(direct.values())[:6],
                       "after_quote_count": sum(r["after_quote"] for r in matching),
                       "headline": matching[0]["title"], "url": matching[0]["sources"][0]["url"]})
    themes.sort(key=lambda t: (any(s["active"] for s in t["related_sectors"]), t["count"], t["latest_at"]), reverse=True)
    result.update(items=items[:200], topics=themes[:8], count=len(items), tracked_ids=[r["id"] for r in items],
                  new_count=sum(r["new_since_midday"] is True for r in items) if can_compare else None,
                  after_quote_count=sum(r["after_quote"] for r in items), archive_limit_reached=len(records) == 5000)
    for item in result["items"]:
        item.pop("priority")
    return result


def refresh_snapshot_news(db, settings, day=None, slot="close"):
    day = day or now().strftime("%Y%m%d")
    parse_day(day)
    if day != now().strftime("%Y%m%d"):
        raise DataError("新闻补充仅支持北京时间当天，不能用当前新闻改写历史报告")
    if not settings.news_enabled or settings.source == "demo":
        raise DataError("请启用ENABLE_NEWS并使用真实数据源")
    saved = db.rows(select(snapshots.c.payload).where(snapshots.c.source == settings.source,
                                                      snapshots.c.day == day, snapshots.c.slot == slot))
    if not saved:
        raise DataError("请先采集该时段的行情快照")
    collection = collect_news(db, settings, force=True)
    from .intraday import export_snapshot
    from .snapshot_analysis import analyze_snapshot

    with db.lock(settings.source):
        saved = db.rows(select(snapshots.c.payload).where(snapshots.c.source == settings.source,
                                                        snapshots.c.day == day, snapshots.c.slot == slot))
        payload = saved[0]["payload"]
        if not payload.get("analysis"):
            reasons = {r["code"]: r["reason"] for r in payload["focus"]}
            metadata = db.rows(select(sessions.c.universe).where(
                sessions.c.source == settings.source, sessions.c.day <= day)
                .order_by(sessions.c.day.desc()).limit(1))
            analyze_snapshot(db, settings, payload, metadata[0]["universe"] if metadata else {})
            payload["focus"] = [{**r, "reason": reasons[r["code"]]} for r in payload["quotes"] if r["code"] in reasons]
        payload["news"] = build_news(db, settings, payload, as_of=now())
        with db.engine.begin() as connection:
            db.upsert(connection, snapshots, {"source": settings.source, "day": day, "slot": slot, "payload": payload})
        artifacts = export_snapshot(payload, settings)
    return {"status": "success", "collection": collection, "news_status": payload["news"]["status"],
            "count": payload["news"]["count"], **artifacts}
