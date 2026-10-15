"""Durable SMTP outbox. Attachments are frozen at enqueue time, not read from latest on retry."""

import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import tempfile
from datetime import timedelta
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
from pathlib import Path

from sqlalchemy import and_, case, select, update
from sqlalchemy.exc import IntegrityError

from .config import now
from .db import mail_outbox, snapshots
from .providers import DataError, parse_day

logger = logging.getLogger(__name__)
SLOTS = {"midday": "午间快照", "close": "收盘快照", "evening": "晚间日线分析"}
MAX_ATTEMPTS = 4
RETRY_DELAY = timedelta(minutes=15)


def configure_gmail(settings, env_path='.env'):
    import getpass

    from dotenv import set_key

    sender = input(f'发件Gmail地址 [{settings.smtp_username}]: ').strip() or settings.smtp_username
    recipient = input(f'收件地址 [{settings.mail_to or sender}]: ').strip() or settings.mail_to or sender
    if not valid_address(sender) or not valid_address(recipient):
        raise DataError('请输入完整的发件和收件邮箱地址')
    password = ''.join(getpass.getpass('Gmail应用专用密码（16个字符，输入不显示）: ').split())
    if len(password) != 16:
        raise DataError('Gmail应用专用密码应为16个字符；请在Google账号中生成后重新填写')
    path = Path(env_path).resolve()
    path.touch(exist_ok=True)
    path.chmod(0o600)
    values = {'SMTP_HOST':'smtp.gmail.com', 'SMTP_PORT':'465', 'SMTP_SECURITY':'ssl',
              'SMTP_USERNAME':sender, 'SMTP_PASSWORD':password, 'MAIL_TO':recipient,
              'EMAIL_ENABLED':'true', 'EXPORT_HTML':'true', 'EXPORT_PNG':'true'}
    for key, value in values.items():
        set_key(path, key, value, quote_mode='always')
    path.chmod(0o600)
    return {'status':'configured', 'path':str(path), 'sent':False,
            'message':'Gmail配置已保存；需重启worker使定时发送生效。可用mail-send发送已有报告验证。'}


def valid_address(value):
    return bool(re.fullmatch(r"[^@\s<>(),;:\r\n]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value))


def configuration_errors(settings, *, attachments=True):
    missing = []
    if not settings.smtp_host or any(c.isspace() for c in settings.smtp_host):
        missing.append('SMTP_HOST')
    if not valid_address(settings.smtp_username):
        missing.append('SMTP_USERNAME')
    if not settings.smtp_password.strip():
        missing.append('SMTP_PASSWORD')
    if not valid_address(settings.mail_to):
        missing.append('MAIL_TO')
    if settings.smtp_security not in {'ssl', 'starttls'}:
        missing.append('SMTP_SECURITY')
    if not 1 <= settings.smtp_port <= 65535 or not 1 <= settings.smtp_timeout <= 60:
        missing.append('SMTP_PORT / SMTP_TIMEOUT')
    if attachments and (not settings.export_html or not settings.export_png):
        missing.append('EXPORT_HTML / EXPORT_PNG（需同时开启）')
    return missing


def require_configuration(settings, *, attachments=True):
    if not settings.email_enabled:
        raise DataError('邮件发送尚未启用；请在本地 .env 设置 EMAIL_ENABLED=true')
    errors = configuration_errors(settings, attachments=attachments)
    if errors:
        raise DataError('请补齐或修正本地邮箱配置：' + '、'.join(errors))


def compose_message(settings, day, slot, html, png, details=None, preview=False):
    parse_day(day)
    if slot not in SLOTS:
        raise DataError('邮件时段必须是 midday、close 或 evening')
    sender = settings.smtp_username if valid_address(settings.smtp_username) else 'preview@example.invalid'
    recipient = settings.mail_to if valid_address(settings.mail_to) else 'recipient@example.invalid'
    identifier = hashlib.sha256(json.dumps([settings.source, day, slot, recipient], separators=(',', ':')).encode()).hexdigest()
    message = EmailMessage(policy=policy.SMTP)
    marker = '【合成示例】' if settings.source == 'demo' else ''
    date = f'{day[:4]}-{day[4:6]}-{day[6:]}'
    cutoff = {'midday':settings.midday_time, 'close':settings.schedule_time, 'evening':settings.announcement_time}[slot]
    message['Subject'] = f'{marker}[收盘之后] {date} {SLOTS[slot]} · {cutoff}'
    message['From'] = Address('收盘之后', addr_spec=sender)
    message['To'] = Address(addr_spec=recipient)
    message['Date'] = format_datetime(now())
    message['Message-ID'] = f'<stock-daily.{identifier}@{sender.rsplit("@", 1)[1]}>'
    details = details or {}
    lines = [f'{date} {SLOTS[slot]}', f'历史与基础信息来源：{settings.source}', f'邮件生成时间：{now().isoformat()}']
    if details.get('quote_start'):
        lines.append(f'供应商报价时间：{details["quote_start"]} ～ {details["quote_end"]}')
    if details.get('captured_at'):
        lines.append(f'报价采集时间：{details["captured_at"]}')
    if details.get('generated_at'):
        lines.append(f'日线报告生成时间：{details["generated_at"]}')
    if slot != 'evening':
        lines.append('实时观察快照，不生成新的正式日线信号；午间成交数据为累计值。')
    lines += ['', '附件PNG为概览长图；HTML可在手机附件预览直接阅读主要数据，用浏览器打开可搜索和筛选。',
              '文件为独立快照，不会自行更新。']
    if preview:
        lines.append('这是本地预览文件，尚未通过SMTP发送。')
    message.set_content('\n'.join(lines))
    for location, maintype, subtype in ((html, 'text', 'html'), (png, 'image', 'png')):
        path = Path(location)
        if not path.is_file() or path.suffix.lower() != '.' + subtype:
            raise DataError('邮件需要同一次任务生成的HTML和PNG文件，请先完成导出')
        if path.stat().st_size > settings.mail_max_mb * 1024 * 1024:
            raise DataError('邮件附件超过 MAIL_MAX_MB 限制，未发送')
        content = path.read_bytes()
        if subtype == 'png' and not content.startswith(b'\x89PNG\r\n\x1a\n'):
            raise DataError('PNG附件格式无效，未发送')
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=path.name)
    raw = message.as_bytes()
    if len(raw) > settings.mail_max_mb * 1024 * 1024:
        raise DataError('编码后的邮件超过 MAIL_MAX_MB 限制，未发送')
    return identifier, message, raw


def enqueue_report(db, settings, day, slot, html, png, details=None):
    if not settings.email_enabled:
        return {'status':'disabled'}
    require_configuration(settings)
    identifier, message, raw = compose_message(settings, day, slot, html, png, details)
    stamp = now().isoformat()
    try:
        with db.engine.begin() as connection:
            connection.execute(mail_outbox.insert().values(
                id=identifier, source=settings.source, day=day, slot=slot, recipient=settings.mail_to,
                status='pending', attempts=0, message=raw, message_id=str(message['Message-ID']),
                subject=str(message['Subject']), created_at=stamp, next_attempt_at=stamp, last_error='',
            ))
    except IntegrityError:
        return {'status':'exists', 'id':identifier}
    return {'status':'queued', 'id':identifier}


def enqueue_safely(db, settings, day, slot, html, png, details=None):
    if not settings.email_enabled:
        return {'status':'disabled'}
    identifier = None
    try:
        identifier = db.start_run(settings.source, day, 'email_queue')
        result = enqueue_report(db, settings, day, slot, html, png, details)
        db.finish_run(identifier, 'skipped' if result['status'] == 'exists' else 'success',
                      '同一时段已在邮件队列中' if result['status'] == 'exists' else 'HTML和PNG邮件已排队')
        return result
    except Exception as error:
        message = str(error) if isinstance(error, DataError) else '邮件排队失败，请检查附件或数据库；分析结果已保留'
        if identifier:
            try:
                db.finish_run(identifier, 'failed', message)
            except Exception:
                pass
        logger.warning('%s', message)
        return {'status':'failed', 'message':message}


def send_smtp(settings, raw, recipient):
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    context = ssl.create_default_context()
    if settings.smtp_security == 'ssl':
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      timeout=settings.smtp_timeout, context=context)
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout)
    try:
        if settings.smtp_security == 'starttls':
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        password = settings.smtp_password
        if settings.smtp_host.lower() == 'smtp.gmail.com':
            password = ''.join(password.split())
        connection.login(settings.smtp_username, password)
        refused = connection.send_message(message, from_addr=message['From'].addresses[0].addr_spec, to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # DATA acceptance is success even if the server disconnects on QUIT.
        # A QUIT failure must not provoke a duplicate resend of an accepted message.
        try:
            connection.quit()
        except Exception:
            connection.close()


def safe_error(error):
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return 'SMTP认证失败，请检查邮箱账号和授权码/应用专用密码'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'SMTP拒绝收件地址，请检查MAIL_TO'
    if isinstance(error, ssl.SSLError):
        return 'SMTP TLS连接或证书验证失败'
    if isinstance(error, smtplib.SMTPException):
        return 'SMTP服务器未确认接收邮件，请检查发信服务；稍后重试'
    if isinstance(error, (OSError, TimeoutError)):
        return 'SMTP连接失败或超时，请检查网络、主机和端口'
    return '邮件发送异常；已保留附件，稍后重试'


def flush_outbox(db, settings, limit=1, identifier=None, *, slots=None):
    if not settings.email_enabled:
        return {'status':'disabled', 'sent':0, 'failed':0}
    require_configuration(settings, attachments=False)
    current = now()
    stale = (current - timedelta(minutes=15)).isoformat()
    with db.engine.begin() as connection:
        connection.execute(update(mail_outbox).where(
            mail_outbox.c.source == settings.source, mail_outbox.c.recipient == settings.mail_to,
            mail_outbox.c.status == 'sending', mail_outbox.c.last_attempt_at < stale,
        ).values(status='failed', last_error='邮件发送进程中断；已保留附件，SMTP接收状态可能无法确认'))
    eligible = and_(mail_outbox.c.source == settings.source,
                    mail_outbox.c.recipient == settings.mail_to, mail_outbox.c.attempts < MAX_ATTEMPTS,
                    mail_outbox.c.next_attempt_at <= current.isoformat(),
                    mail_outbox.c.status.in_(['pending', 'failed']))
    if not settings.trade_email_enabled:
        eligible = and_(eligible, mail_outbox.c.slot != 'trade')
    if slots:
        eligible = and_(eligible, mail_outbox.c.slot.in_(slots))
    query = select(mail_outbox).where(eligible).order_by(
        case((mail_outbox.c.slot == 'trade', 0), else_=1), mail_outbox.c.created_at).limit(limit)
    if identifier:
        query = query.where(mail_outbox.c.id == identifier)
    sent = failed = 0
    for record in db.rows(query):
        with db.engine.begin() as connection:
            claimed = connection.execute(update(mail_outbox).where(mail_outbox.c.id == record['id'], eligible)
                                         .values(status='sending', attempts=mail_outbox.c.attempts + 1,
                                                 last_attempt_at=current.isoformat()))
            if claimed.rowcount != 1:
                continue
        task = db.start_run(settings.source, record['day'], 'email')
        try:
            send_smtp(settings, record['message'], record['recipient'])
        except Exception as error:
            message = safe_error(error)
            with db.engine.begin() as connection:
                connection.execute(update(mail_outbox).where(mail_outbox.c.id == record['id'])
                                   .values(status='failed', last_error=message,
                                           next_attempt_at=(now() + RETRY_DELAY).isoformat()))
            db.finish_run(task, 'failed', message)
            logger.warning('%s', message)
            failed += 1
        else:
            with db.engine.begin() as connection:
                connection.execute(update(mail_outbox).where(mail_outbox.c.id == record['id'])
                                   .values(status='sent', sent_at=now().isoformat(), last_error='', message=b''))
            db.finish_run(task, 'success', 'SMTP服务器已接受模拟成交提醒' if record['slot'] == 'trade' else 'SMTP服务器已接受HTML和PNG报告邮件')
            sent += 1
    return {'status':'failed' if failed else 'success', 'sent':sent, 'failed':failed}


def outbox_status(db, settings):
    columns = [mail_outbox.c[name] for name in ('id', 'day', 'slot', 'status', 'attempts', 'created_at',
                                               'next_attempt_at', 'sent_at', 'last_error')]
    return {'enabled':settings.email_enabled, 'configuration_errors':configuration_errors(settings),
            'items':db.rows(select(*columns).where(mail_outbox.c.source == settings.source)
                            .order_by(mail_outbox.c.created_at.desc()).limit(30))}


def retry_message(db, settings, identifier):
    require_configuration(settings)
    with db.engine.begin() as connection:
        result = connection.execute(update(mail_outbox)
                                    .where(mail_outbox.c.id == identifier, mail_outbox.c.source == settings.source,
                                           mail_outbox.c.recipient == settings.mail_to, mail_outbox.c.status == 'failed')
                                    .values(status='pending', attempts=0, next_attempt_at=now().isoformat(), last_error=''))
        if result.rowcount != 1:
            raise DataError('只能重试当前收件地址的失败邮件；不存在、已成功或发送中的邮件不能重复发送')
    return flush_outbox(db, settings, identifier=identifier)


def saved_artifacts(db, settings, day=None, slot='midday'):
    if slot not in SLOTS:
        raise DataError('邮件时段必须是 midday、close 或 evening')
    if day:
        parse_day(day)
    folder = Path(settings.html_report_dir)
    if slot == 'evening':
        details = db.latest_report(settings.source, day)
        if not details:
            raise DataError('没有指定日期的已发布日线报告')
        day = details['day']
        # Prefer the completed evening copy; a manually published daily report is
        # also usable when explicitly requested, retaining its genuine date.
        path = folder / f'{settings.source}-{day}-{settings.announcement_time.replace(":", "")}.html'
        if not path.is_file():
            path = folder / f'{settings.source}-{day}.html'
    else:
        query = select(snapshots.c.payload).where(snapshots.c.source == settings.source, snapshots.c.slot == slot)
        if day:
            query = query.where(snapshots.c.day == day)
        saved = db.rows(query.order_by(snapshots.c.day.desc()).limit(1))
        if not saved:
            raise DataError('没有指定日期和时段的已保存实时快照')
        details = saved[0]['payload']
        day = details['day']
        cutoff = details.get('scheduled_time') or (settings.midday_time if slot == 'midday' else settings.schedule_time)
        path = folder / f'{settings.source}-{day}-{cutoff.replace(":", "")}.html'
    return day, path, path.with_suffix('.png'), details


def preview_report(db, settings, day=None, slot='midday', output='data/mail-preview.eml'):
    day, html, png, details = saved_artifacts(db, settings, day, slot)
    _, message, raw = compose_message(settings, day, slot, html, png, details, preview=True)
    destination = Path(output).resolve()
    if destination.suffix.lower() != '.eml':
        raise DataError('邮件预览文件必须以.eml结尾')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as file:
            temporary = file.name
            file.write(raw)
        os.replace(temporary, destination)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
    return {'status':'preview', 'path':str(destination), 'subject':str(message['Subject']), 'bytes':len(raw), 'sent':False}
