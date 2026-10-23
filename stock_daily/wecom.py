"""Private WeCom long-connection trade delivery, independent of SMTP and quotes.

Protocol reference: https://github.com/WecomTeam/aibot-node-sdk
Only authenticated acknowledgments mark a notification sent. Incoming text can
bind a destination with a one-use local pairing code; it never operates trades.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .config import now
from .db import trade_alerts, wecom_outbox
from .operations import state
from .providers import DataError

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 4
ACK_TIMEOUT = 10
HEARTBEAT_SECONDS = 30
DELAYED_NOTICE = "**补发成交提醒：以原成交时间为准，未重新交易。**\n"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def enabled(settings):
    return bool(settings.wecom_bot_id and settings.wecom_bot_secret)


def target(db, settings):
    saved = state(db, settings, "wecom_target")
    bot_hash = digest(settings.wecom_bot_id)
    if settings.wecom_chat_id:
        if saved.get("bot_hash") != bot_hash or saved.get("chat_id") != settings.wecom_chat_id:
            saved = {"bot_hash": bot_hash, "chat_id": settings.wecom_chat_id,
                "chat_type": settings.wecom_chat_type, "bound_at": now().isoformat(),
                "since_day": now().strftime("%Y%m%d")}
            db.save_state(settings.source, "wecom_target", saved)
        return saved
    return saved if saved.get("bot_hash") == bot_hash and saved.get("chat_id") else None


def prepare_pairing(db, settings, *, force=False):
    if not enabled(settings) or not settings.wecom_pair_code or target(db, settings):
        return
    saved = state(db, settings, "wecom_pairing")
    code_hash = digest(settings.wecom_pair_code)
    if saved.get("bot_hash") == digest(settings.wecom_bot_id) and not force:
        return  # Expired or used codes are never automatically reactivated.
    db.save_state(settings.source, "wecom_pairing", {"bot_hash": digest(settings.wecom_bot_id),
        "code_hash": code_hash, "expires_at": (now() + timedelta(minutes=30)).isoformat(), "used": False})


def create_pairing(db, settings):
    if not enabled(settings):
        raise DataError("请先在本地配置WECOM_BOT_ID和WECOM_BOT_SECRET")
    if target(db, settings):
        return {"status": "bound", "message": "已有通知接收会话，无需重复绑定"}
    code = secrets.token_hex(6)
    prepare_pairing(db, replace(settings, wecom_pair_code=code), force=True)
    return {"status": "pairing", "command": "订阅交易提醒 " + code,
        "expires_at": state(db, settings, "wecom_pairing")["expires_at"],
        "message": "在目标机器人会话中发送上述口令；无需重启worker"}


def bind_callback(db, settings, frame):
    if frame.get("cmd") != "aibot_msg_callback" or target(db, settings):
        return None
    body = frame.get("body", {})
    if (body.get("aibotid") != settings.wecom_bot_id or body.get("msgtype") != "text"
            or body.get("chattype") != settings.wecom_chat_type):
        return None
    content = body.get("text", {}).get("content", "")
    words = content.split() if isinstance(content, str) else []
    if len(words) != 2 or words[0] != "订阅交易提醒":
        return None
    code = words[1]
    saved = state(db, settings, "wecom_pairing")
    if (saved.get("used") or saved.get("bot_hash") != digest(settings.wecom_bot_id)
            or not saved.get("expires_at") or datetime.fromisoformat(saved["expires_at"]) <= now()
            or not hmac.compare_digest(saved.get("code_hash", ""), digest(code.strip()))):
        return None
    chat_id = body.get("chatid") if settings.wecom_chat_type == "group" else body.get("from", {}).get("userid")
    if not isinstance(chat_id, str) or not chat_id.strip() or len(chat_id) > 320:
        return None
    bound = {"bot_hash": digest(settings.wecom_bot_id), "chat_id": chat_id,
        "chat_type": settings.wecom_chat_type, "bound_at": now().isoformat(), "since_day": now().strftime("%Y%m%d")}
    # The worker has one connection; lock also prevents a simultaneous manual binder.
    with db.lock("wecom-bind:" + settings.source):
        if target(db, settings):
            return None
        db.save_state(settings.source, "wecom_target", bound)
        db.save_state(settings.source, "wecom_pairing", {**saved, "used": True})
    return bound


def trade_content(fill):
    side = "买入" if fill["side"] == "buy" else "卖出"
    def clean(value):
        return str(value or "").replace("\n", " ").replace("\r", " ").replace("`", "'")
    lines = [f"## 模拟成交 · {side}", f"**{clean(fill['name'])}（{fill['code']}）**",
        f"成交价格：{fill['price']:.4f} 元", f"成交股数：{fill['shares']} 股",
        f"成交金额：{fill['notional']:.2f} 元", f"本笔费用：{fill['fee']:.2f} 元",
        f"现金变化：{fill['cash_flow']:+.2f} 元", f"成交后现金：{fill['cash_after']:.2f} 元",
        f"成交原因：{clean(fill['reason'])}", f"策略：{clean(fill.get('label') or fill['strategy'])}"]
    if fill["side"] == "sell":
        lines.append(f"该轮扣费已实现盈亏：{fill['realized_profit']:+.2f} 元")
        if fill.get("exit_slippage_below_low"):
            lines.append(f"滑点模拟：卖价低于已观察日内最低 {fill['exit_observed_low']:.4f} 元，不代表真实撮合")
    if fill.get('signal_origin') == 'intraday':
        lines.append(f"候选来源：盘中新发现 · 首次发现 {clean(fill.get('discovered_at'))}")
    if fill.get("entry_confirmation"):
        e = fill["entry_confirmation"]
        lines += [f"盘中买入确认：{e['version']} · {clean(e['reason'])}",
            f"当日成交均价：{e['vwap']:.4f} 元 · 区间新增成交 {e['interval_volume']:.0f} 股",
            f"区间相对沪深300：{e['relative_interval']:+.2f} 个百分点"]
        if e.get('max_entry_gap') is not None:
            lines.append(f"本次追高上限：{e['max_entry_gap']:.2%} · 回落容忍度：{e['max_quote_pullback']:.2%}")
            if e.get('confirmation_mode') == 'gap_recovery':
                lines.append("确认方式：高开回落后重新确认")
    if fill.get('decision_context'):
        d = fill['decision_context']
        lines += [f"AI/舆情依据：{clean(d.get('reason'))}", f"AI依据截止：{clean(d.get('as_of'))}"]
    if fill.get("exit_policy"):
        p = fill["exit_policy"]
        lines.append(f"退出保护：{p['version']} · 保护价 {p['stop']:.4f} 元")
    lines += [f"报价时间：{fill['execution_time']}（北京时间）", f"确认时间：{fill['confirmed_at']}",
        f"成交编号：{fill['id']}", "仅为模拟成交记录。"]
    return "\n".join(lines)


def queue_notifications(db, settings):
    bound = target(db, settings) if enabled(settings) else None
    if not bound:
        return 0
    target_hash = digest(settings.wecom_bot_id + "|" + bound["chat_id"])
    # First binding includes today's confirmed fills. Older archives are not broadcast.
    events = db.rows(select(trade_alerts).outerjoin(wecom_outbox,
        (wecom_outbox.c.event_id == trade_alerts.c.id) & (wecom_outbox.c.target_hash == target_hash))
        .where(trade_alerts.c.source == settings.source, trade_alerts.c.day >= bound["since_day"],
               wecom_outbox.c.id.is_(None)).order_by(trade_alerts.c.created_at).limit(100))
    queued = 0
    for event in sorted(events, key=lambda e: (e["created_at"], e["payload"]["sequence"])):
        identifier = digest(event["id"] + "|" + target_hash)
        current = now()
        stamp = current.isoformat()
        content = trade_content(event["payload"])
        if (event["created_at"] < bound["bound_at"]
                or datetime.fromisoformat(event["created_at"]) < current - timedelta(minutes=2)):
            content = DELAYED_NOTICE + content
        try:
            with db.engine.begin() as connection:
                connection.execute(wecom_outbox.insert().values(id=identifier, source=settings.source,
                    event_id=event["id"], target_hash=target_hash, chat_id=bound["chat_id"], content=content,
                    status="pending", attempts=0, created_at=stamp, next_attempt_at=stamp, last_error=""))
        except IntegrityError:
            continue
        queued += 1
    return queued


class ProtocolError(Exception):
    def __init__(self, code=None):
        self.code = code if isinstance(code, int) else None
        super().__init__("企业微信未确认请求" + (f"（错误码{self.code}）" if self.code is not None else ""))


class ConnectionReplaced(Exception):
    pass


class Client:
    def __init__(self, websocket, db, settings, stopped):
        self.websocket, self.db, self.settings, self.stopped = websocket, db, settings, stopped
        self.confirm = None

    def callback(self, frame):
        body = frame.get("body") or {}
        content = (body.get("text") or {}).get("content", "")
        self.db.save_state(self.settings.source, "wecom_inbound_status", {
            "at": now().isoformat(), "command": frame.get("cmd"), "body_keys": sorted(body),
            "message_type": body.get("msgtype"), "chat_type": body.get("chattype"),
            "bot_matches": body.get("aibotid") == self.settings.wecom_bot_id,
            "has_sender": bool((body.get("from") or {}).get("userid")),
            "pair_command_matches": isinstance(content, str) and content.strip().startswith("订阅交易提醒")})
        if (frame.get("cmd") == "aibot_event_callback"
                and frame.get("body", {}).get("event", {}).get("eventtype") == "disconnected_event"):
            raise ConnectionReplaced
        bound = bind_callback(self.db, self.settings, frame)
        if bound:
            self.confirm = bound

    def receive(self, timeout):
        frame = json.loads(self.websocket.recv(timeout=timeout))
        if not isinstance(frame, dict):
            raise ProtocolError
        if frame.get("cmd") in {"aibot_msg_callback", "aibot_event_callback"}:
            self.callback(frame)
        return frame

    def request(self, command, body=None, request_id=None):
        request_id = request_id or command + "_" + uuid.uuid4().hex
        frame = {"cmd": command, "headers": {"req_id": request_id}}
        if body is not None:
            frame["body"] = body
        self.websocket.send(json.dumps(frame, ensure_ascii=False))
        deadline = time.monotonic() + ACK_TIMEOUT
        while not self.stopped.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            response = self.receive(remaining)
            if response.get("cmd"):
                continue
            if response.get("headers", {}).get("req_id") == request_id:
                if response.get("errcode") != 0:
                    raise ProtocolError(response.get("errcode"))
                return response
        raise TimeoutError

    def send(self, chat_id, content, request_id=None):
        return self.request("aibot_send_msg", {"chatid": chat_id,
            "msgtype": "markdown", "markdown": {"content": content}}, request_id)


def deliver_one(db, settings, client):
    bound = target(db, settings)
    if not bound:
        return False
    current = now()
    scope = digest(settings.wecom_bot_id + "|" + bound["chat_id"])
    # A crashed sender is retried after a lease; preserve the message ID on retry.
    with db.engine.begin() as connection:
        connection.execute(update(wecom_outbox).where(wecom_outbox.c.source == settings.source,
            wecom_outbox.c.status == "sending", wecom_outbox.c.last_attempt_at < (current - timedelta(minutes=2)).isoformat())
            .values(status="failed", last_error="连接中断，接收状态待核验"))
    eligible = (wecom_outbox.c.source == settings.source) & (wecom_outbox.c.target_hash == scope) & (
        wecom_outbox.c.status.in_(["pending", "failed"])) & (wecom_outbox.c.attempts < MAX_ATTEMPTS) & (
        wecom_outbox.c.next_attempt_at <= current.isoformat())
    rows = db.rows(select(wecom_outbox).where(eligible).order_by(wecom_outbox.c.created_at, wecom_outbox.c.event_id).limit(1))
    if not rows:
        return False
    record = rows[0]
    content = record["content"]
    if (record["attempts"] == 0 and not content.startswith(DELAYED_NOTICE)
            and datetime.fromisoformat(record["created_at"]) < current - timedelta(minutes=2)):
        content = DELAYED_NOTICE + content
    with db.engine.begin() as connection:
        claimed = connection.execute(update(wecom_outbox).where(wecom_outbox.c.id == record["id"], eligible)
            .values(status="sending", attempts=wecom_outbox.c.attempts + 1,
                    last_attempt_at=current.isoformat(), content=content))
        if claimed.rowcount != 1:
            return False
    try:
        client.send(record["chat_id"], content, "trade_" + record["id"][:32])
    except Exception as error:
        message = str(error) if isinstance(error, ProtocolError) else "企业微信发送失败或确认超时，将重试（可能已接收，以成交编号核对）"
        with db.engine.begin() as connection:
            connection.execute(update(wecom_outbox).where(wecom_outbox.c.id == record["id"]).values(
                status="failed", last_error=message,
                next_attempt_at=(current + timedelta(minutes=2 ** record["attempts"])).isoformat()))
        if not isinstance(error, ProtocolError):
            raise
    else:
        with db.engine.begin() as connection:
            connection.execute(update(wecom_outbox).where(wecom_outbox.c.id == record["id"]).values(
                status="sent", sent_at=now().isoformat(), last_error=""))
    return True


def status_view(db, settings):
    connection = state(db, settings, "wecom_connection") if enabled(settings) else {"status": "disabled"}
    counts = {r["status"]: r["count"] for r in db.rows(select(wecom_outbox.c.status, func.count().label("count"))
        .where(wecom_outbox.c.source == settings.source).group_by(wecom_outbox.c.status))}
    if connection.get("status") == "authenticated" and connection.get("at"):
        if now() - datetime.fromisoformat(connection["at"]) > timedelta(seconds=90):
            connection = {**connection, "status": "stale", "message": "长连接心跳已过期"}
    columns = [wecom_outbox.c[k] for k in ("id", "status", "attempts", "created_at", "sent_at", "last_error")]
    items = db.rows(select(*columns).where(wecom_outbox.c.source == settings.source)
        .order_by(wecom_outbox.c.created_at.desc()).limit(20))
    return {"enabled": enabled(settings), "items": items, "bound": bool(target(db, settings)) if enabled(settings) else False,
        "connection": connection, "counts": counts}


def retry_notification(db, settings, identifier):
    bound = target(db, settings) if enabled(settings) else None
    if not bound:
        raise DataError("企业微信尚未配置或未绑定通知会话")
    scope = digest(settings.wecom_bot_id + "|" + bound["chat_id"])
    with db.engine.begin() as connection:
        changed = connection.execute(update(wecom_outbox).where(wecom_outbox.c.id == identifier,
            wecom_outbox.c.source == settings.source, wecom_outbox.c.target_hash == scope,
            wecom_outbox.c.status == "failed").values(status="pending", attempts=0,
                next_attempt_at=now().isoformat(), last_error=""))
        if changed.rowcount != 1:
            raise DataError("只能重试当前通知会话的失败消息；已成功或正在发送的消息不重复发送")
    return {"status": "pending", "message": "已重新排队，等待后台长连接发送"}


def run(db, settings, stopped):
    if not enabled(settings):
        return
    from websockets.sync.client import connect

    prepare_pairing(db, settings)
    delay = 1
    replaced = False
    transport = logging.getLogger("stock_daily.wecom.transport")
    transport.setLevel(logging.WARNING)  # Never log authentication frames even under a debug root logger.
    while not stopped.is_set():
        # Persist pending notifications even while disconnected or displaced.
        # A displaced connection stays paused until an operator restarts it;
        # never fight another client by repeatedly taking the connection back.
        try:
            queue_notifications(db, settings)
        except Exception:
            logger.warning("企业微信提醒入队失败，将重试", exc_info=True)
        if replaced:
            stopped.wait(5)
            continue
        try:
            with connect(settings.wecom_ws_url, open_timeout=10, close_timeout=2,
                         ping_interval=None, max_size=1_048_576, logger=transport) as websocket:
                client = Client(websocket, db, settings, stopped)
                client.request("aibot_subscribe", {"bot_id": settings.wecom_bot_id, "secret": settings.wecom_bot_secret})
                delay = 1
                heartbeat = time.monotonic()
                checked = 0
                while not stopped.is_set():
                    if client.confirm:
                        client.send(client.confirm["chat_id"], "已绑定模拟交易提醒：后续已确认的买入、卖出会推送到此会话；邮件提醒继续发送。")
                        client.confirm = None
                    if time.monotonic() - checked >= 5:
                        queue_notifications(db, settings)
                        db.save_state(settings.source, "wecom_connection", {"status": "authenticated", "at": now().isoformat(), "pid": os.getpid()})
                        checked = time.monotonic()
                    deliver_one(db, settings, client)
                    if time.monotonic() - heartbeat >= HEARTBEAT_SECONDS:
                        client.request("ping")
                        heartbeat = time.monotonic()
                    try:
                        client.receive(timeout=1)
                    except TimeoutError:
                        pass
        except ConnectionReplaced:
            db.save_state(settings.source, "wecom_connection", {"status": "conflict", "at": now().isoformat(),
                "message": "机器人被另一个长连接接管；已保留待发提醒，避免互相抢连，需关闭重复客户端后重启worker"})
            logger.warning("企业微信长连接被接管，暂停连接；继续保留新成交提醒，等待恢复")
            replaced = True
            continue
        except Exception as error:
            message = str(error) if isinstance(error, ProtocolError) else "企业微信连接暂不可用，自动重连；成交和邮件不受影响"
            db.save_state(settings.source, "wecom_connection", {"status": "reconnecting", "at": now().isoformat(), "message": message})
            logger.warning("%s", message)
        stopped.wait(delay)
        delay = min(delay * 2, 30)

    if stopped.is_set():
        db.save_state(settings.source, "wecom_connection", {"status": "stopped", "at": now().isoformat()})
