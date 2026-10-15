"""One durable notification event per confirmed fill, independent of report export."""

import hashlib
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from html import escape

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import now
from .db import mail_outbox, trade_alerts
from .mailer import require_configuration


def record_alerts(connection, settings, fills, current):
    """Called in the account commit: a rollback also removes notification events."""
    if not settings.trade_email_enabled and not (settings.wecom_bot_id and settings.wecom_bot_secret):
        return
    for fill in fills:
        identifier = hashlib.sha256(("trade:" + fill["id"]).encode()).hexdigest()
        connection.execute(trade_alerts.insert().values(id=identifier, source=settings.source,
            day=fill["day"], payload=fill, created_at=current.isoformat()))


def compose_trade(settings, event):
    fill = event["payload"]
    side = "买入" if fill["side"] == "buy" else "卖出"
    message = EmailMessage(policy=policy.SMTP)
    name = " ".join(str(fill["name"]).split())
    message["Subject"] = f"[模拟成交·{side}] {name}（{fill['code']}）{fill['shares']}股 · {fill['day']}"
    message["From"] = Address("观市", addr_spec=settings.smtp_username)
    message["To"] = Address(addr_spec=settings.mail_to)
    message["Date"] = format_datetime(now())
    message["Message-ID"] = f"<stock-daily.trade.{event['id']}@{settings.smtp_username.rsplit('@', 1)[1]}>"
    facts = [("成交方向", "模拟" + side), ("股票", f"{fill['name']}（{fill['code']}）"),
        ("模拟成交价", f"{fill['price']:.4f} 元"), ("股数", f"{fill['shares']} 股"),
        ("成交金额", f"{fill['notional']:.2f} 元"), ("本笔费用", f"{fill['fee']:.2f} 元"),
        ("现金变化", f"{fill['cash_flow']:+.2f} 元"), ("成交后现金", f"{fill['cash_after']:.2f} 元"),
        ("报价时间（北京时间）", fill["execution_time"]), ("确认时间（北京时间）", fill["confirmed_at"]),
        ("成交原因", fill["reason"]), ("策略", fill.get("label") or fill["strategy"]),
        ("信号日期", fill["signal_day"]), ("规则版本", fill.get("rule_version") or "未记录"),
        ("成交编号", fill["id"])]
    if fill["side"] == "sell":
        facts.append(("该轮交易已实现盈亏（扣双边费用）", f"{fill['realized_profit']:+.2f} 元"))
    entry = fill.get("entry_confirmation")
    if entry:
        facts += [("盘中买入规则", entry["version"]), ("买入确认依据", entry["reason"]),
                  ("观察区间", f"{entry['previous_quote_time']} ～ {entry['quote_time']}"),
                  ("当日成交均价", f"{entry['vwap']:.4f} 元"),
                  ("区间新增成交量", f"{entry['interval_volume']:.0f} 股"),
                  ("区间相对沪深300", f"{entry['relative_interval']:+.2f} 个百分点")]
    decision = fill.get('decision_context')
    if decision:
        facts += [('AI/舆情决策', decision.get('reason', '')), ('AI依据截止', decision.get('as_of', '')),
                  ('AI模型与版本', f"{decision.get('model', '')} / {decision.get('version', '')}")]
    protection = fill.get("exit_policy")
    if protection:
        facts += [("退出规则", protection["version"]), ("当前保护价", f"{protection['stop']:.4f} 元"),
                  ("已观察报价高点", f"{protection['peak']:.4f} 元")]
    if fill.get("exit_trigger_time"):
        facts.append(("退出触发时间", fill["exit_trigger_time"]))
    if fill.get("notification_delayed"):
        facts.insert(0, ("提醒类型", "启用成交邮件后补发；实际成交时间以以下记录为准，未重新交易"))
    note = "这是模拟账户已确认成交提醒，价格已计滑点；实际资金未发生证券交易。"
    message.set_content("\n".join(f"{key}：{value}" for key, value in facts) + "\n\n" + note)
    rows = "".join(f"<tr><th style='text-align:left;padding:8px'>{escape(key)}</th>"
                   f"<td style='padding:8px;overflow-wrap:anywhere'>{escape(str(value))}</td></tr>" for key, value in facts)
    message.add_alternative(f"<!doctype html><html lang='zh-CN'><meta name='viewport' content='width=device-width'>"
        f"<body style='font-family:sans-serif;max-width:640px;margin:auto;padding:16px'>"
        f"<h2>模拟{side}成交提醒</h2><table style='width:100%;border-collapse:collapse'>{rows}</table>"
        f"<p>{note}</p></body></html>", subtype="html")
    return message


def queue_trade_alerts(db, settings, limit=100):
    if not settings.email_enabled or not settings.trade_email_enabled:
        return {"status": "disabled", "queued": 0}
    require_configuration(settings, attachments=False)
    pending = db.rows(select(trade_alerts).outerjoin(mail_outbox, trade_alerts.c.id == mail_outbox.c.id)
        .where(trade_alerts.c.source == settings.source, mail_outbox.c.id.is_(None))
        .order_by(trade_alerts.c.created_at, trade_alerts.c.id).limit(limit))
    queued = 0
    for event in sorted(pending, key=lambda e: (e["created_at"], e["payload"]["sequence"])):
        message = compose_trade(settings, event)
        stamp = now().isoformat()
        try:
            with db.engine.begin() as connection:
                connection.execute(mail_outbox.insert().values(id=event["id"], source=settings.source,
                    day=event["day"], slot="trade", recipient=settings.mail_to, status="pending", attempts=0,
                    message=message.as_bytes(), message_id=str(message["Message-ID"]),
                    subject=str(message["Subject"]), created_at=stamp, next_attempt_at=stamp, last_error=""))
        except IntegrityError:
            continue
        queued += 1
    return {"status": "success", "queued": queued}
