import base64
import smtplib
import ssl
from dataclasses import replace
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest
from sqlalchemy import select, update

from stock_daily.config import SHANGHAI
from stock_daily.db import Database, mail_outbox, runs
from stock_daily.mailer import (
    compose_message,
    configuration_errors,
    enqueue_report,
    enqueue_safely,
    flush_outbox,
    outbox_status,
    retry_message,
    send_smtp,
)
from stock_daily.providers import DataError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGFkAAAAASUVORK5CYII=')


@pytest.fixture
def mail_settings(settings):
    return replace(settings, email_enabled=True, export_html=True, export_png=True,
                   smtp_username='owner@example.com', smtp_password='private-password-sentinel',
                   mail_to='recipient@example.com')


@pytest.fixture
def files(tmp_path):
    html = tmp_path / 'baostock-20261012-1135.html'
    png = html.with_suffix('.png')
    html.write_text('<html lang="zh-CN">真实报告</html>')
    png.write_bytes(PNG)
    return html, png


def test_mime_preserves_both_attachments_without_password(mail_settings, files):
    _, message, raw = compose_message(mail_settings, '20261012', 'midday', *files,
                                     {'quote_start':'2026-10-12T11:30:00+08:00',
                                      'quote_end':'2026-10-12T11:30:00+08:00'})
    decoded = BytesParser(policy=policy.default).parsebytes(raw)
    assert '2026-10-12' in str(decoded['Subject']) and '午间' in str(decoded['Subject'])
    assert '11:30:00' in decoded.get_body(preferencelist=('plain',)).get_content()
    attachments = list(decoded.iter_attachments())
    assert [a.get_content_type() for a in attachments] == ['text/html', 'image/png']
    assert [a.get_filename() for a in attachments] == [p.name for p in files]
    assert [a.get_payload(decode=True) for a in attachments] == [p.read_bytes() for p in files]
    assert mail_settings.smtp_password.encode() not in raw
    assert len(message['Message-ID']) < 150


def test_queue_is_disabled_by_default(settings, db, files):
    assert enqueue_report(db, settings, '20261012', 'midday', *files)['status'] == 'disabled'
    assert db.rows(select(mail_outbox)) == []
    assert flush_outbox(db, settings)['status'] == 'disabled'


def test_once_per_source_day_slot_recipient_and_restart_safe_attachments(mail_settings, db, files, monkeypatch):
    first = enqueue_report(db, mail_settings, '20261012', 'midday', *files)
    assert enqueue_report(db, mail_settings, '20261012', 'midday', *files)['status'] == 'exists'
    files[0].write_text('later report replaced the source file')
    reopened = Database(mail_settings.database_url)
    sent = []
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda settings, raw, recipient: sent.append((raw, recipient)))
    assert flush_outbox(reopened, mail_settings)['sent'] == 1
    assert b'later report' not in sent[0][0]
    assert sent[0][1] == mail_settings.mail_to
    assert flush_outbox(reopened, mail_settings)['sent'] == 0
    record = reopened.rows(select(mail_outbox).where(mail_outbox.c.id == first['id']))[0]
    assert record['status'] == 'sent' and record['attempts'] == 1
    assert record['message'] == b''  # Free queued attachment space after acceptance.
    with pytest.raises(DataError, match='不能重复'):
        retry_message(reopened, mail_settings, first['id'])
    reopened.engine.dispose()


def test_failures_retry_without_analysis_and_never_log_smtp_secrets(mail_settings, db, files, monkeypatch):
    clock = [datetime(2026, 10, 12, 12, 0, tzinfo=SHANGHAI)]
    monkeypatch.setattr('stock_daily.mailer.now', lambda: clock[0])
    enqueue_report(db, mail_settings, '20261012', 'midday', *files)
    calls = []
    def fail(*args):
        calls.append(1)
        raise smtplib.SMTPAuthenticationError(535, mail_settings.smtp_password.encode())
    monkeypatch.setattr('stock_daily.mailer.send_smtp', fail)
    for attempt in range(4):
        result = flush_outbox(db, mail_settings)
        assert result['failed'] == 1
        assert flush_outbox(db, mail_settings)['failed'] == 0  # Retry delay is durable.
        clock[0] += timedelta(minutes=16)
    assert flush_outbox(db, mail_settings)['failed'] == 0
    assert len(calls) == 4
    record = db.rows(select(mail_outbox))[0]
    assert record['attempts'] == 4 and record['status'] == 'failed' and record['message']
    assert '认证失败' in record['last_error']
    assert mail_settings.smtp_password not in str(outbox_status(db, mail_settings))
    assert all(mail_settings.smtp_password not in task['message'] for task in db.rows(select(runs)))
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda *args: None)
    assert retry_message(db, mail_settings, record['id'])['sent'] == 1


def test_delivery_is_source_and_recipient_scoped(mail_settings, db, files, monkeypatch):
    enqueue_report(db, mail_settings, '20261012', 'midday', *files)
    def forbidden(*args):
        raise AssertionError('不同来源或收件人不得发送当前队列')
    monkeypatch.setattr('stock_daily.mailer.send_smtp', forbidden)
    assert flush_outbox(db, replace(mail_settings, source='baostock'))['sent'] == 0
    assert flush_outbox(db, replace(mail_settings, mail_to='changed@example.com'))['sent'] == 0
    assert db.rows(select(mail_outbox))[0]['attempts'] == 0


def test_active_sending_is_not_claimed_again_and_stale_exhausted_send_can_be_retried(mail_settings, db, files, monkeypatch):
    identifier = enqueue_report(db, mail_settings, '20261012', 'midday', *files)['id']
    current = datetime(2026, 10, 12, 12, 0, tzinfo=SHANGHAI)
    monkeypatch.setattr('stock_daily.mailer.now', lambda: current)
    with db.engine.begin() as connection:
        connection.execute(update(mail_outbox).where(mail_outbox.c.id == identifier)
                           .values(status='sending', attempts=1, last_attempt_at=current.isoformat()))
    assert flush_outbox(db, mail_settings)['sent'] == 0
    with db.engine.begin() as connection:
        connection.execute(update(mail_outbox).where(mail_outbox.c.id == identifier)
                           .values(attempts=4, last_attempt_at=(current - timedelta(minutes=16)).isoformat()))
    assert flush_outbox(db, mail_settings)['sent'] == 0
    assert db.rows(select(mail_outbox))[0]['status'] == 'failed'
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda *args: None)
    assert retry_message(db, mail_settings, identifier)['sent'] == 1


def test_configuration_or_attachment_errors_do_not_invalidate_report(populated, files):
    db, settings, _ = populated
    before = db.latest_report('demo')
    result = enqueue_safely(db, replace(settings, email_enabled=True), '20261009', 'evening', *files)
    assert result['status'] == 'failed'
    assert db.latest_report('demo') == before
    assert db.rows(select(mail_outbox)) == []
    assert db.rows(select(runs))[-1]['kind'] == 'email_queue'


def test_invalid_attachment_and_large_mail_are_not_queued(mail_settings, db, files):
    files[1].write_bytes(b'not png')
    with pytest.raises(DataError, match='PNG附件格式'):
        enqueue_report(db, mail_settings, '20261012', 'midday', *files)
    files[1].write_bytes(PNG)
    files[0].write_text('a' * (1024 * 1024))
    with pytest.raises(DataError, match='邮件超过'):
        enqueue_report(db, replace(mail_settings, mail_max_mb=1), '20261012', 'midday', *files)
    assert db.rows(select(mail_outbox)) == []


def test_address_header_injection_rejected(mail_settings):
    assert 'MAIL_TO' in configuration_errors(replace(mail_settings, mail_to='me@example.com\r\nBcc:other@example.com'))


class FakeSMTP:
    def __init__(self):
        self.events = []
        self.quit_error = False
    def ehlo(self):
        self.events.append('ehlo')
    def starttls(self, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        self.events.append('tls')
    def login(self, username, password):
        self.events.append(('login', username, password))
    def send_message(self, message, from_addr, to_addrs):
        self.events.append(('send', from_addr, to_addrs))
        return {}
    def quit(self):
        if self.quit_error:
            raise smtplib.SMTPServerDisconnected('quit failed')
    def close(self):
        self.events.append('closed')


def test_starttls_before_credentials_and_quit_failure_does_not_duplicate(mail_settings, files, monkeypatch):
    _, _, raw = compose_message(mail_settings, '20261012', 'midday', *files)
    smtp = FakeSMTP()
    smtp.quit_error = True
    monkeypatch.setattr('stock_daily.mailer.smtplib.SMTP', lambda *args, **kwargs: smtp)
    settings = replace(mail_settings, smtp_security='starttls', smtp_port=587, smtp_password='abcd efgh ijkl mnop')
    send_smtp(settings, raw, settings.mail_to)
    assert smtp.events[:3] == ['ehlo', 'tls', 'ehlo']
    assert smtp.events[3] == ('login', settings.smtp_username, 'abcdefghijklmnop')
    assert smtp.events[4] == ('send', settings.smtp_username, [settings.mail_to])
    assert smtp.events[5] == 'closed'


def test_failed_tls_never_sends_password(mail_settings, files, monkeypatch):
    _, _, raw = compose_message(mail_settings, '20261012', 'midday', *files)
    smtp = FakeSMTP()
    def broken_tls(context):
        raise smtplib.SMTPNotSupportedError('STARTTLS unsupported')
    smtp.starttls = broken_tls
    monkeypatch.setattr('stock_daily.mailer.smtplib.SMTP', lambda *args, **kwargs: smtp)
    with pytest.raises(smtplib.SMTPNotSupportedError):
        send_smtp(replace(mail_settings, smtp_security='starttls', smtp_port=587), raw, mail_settings.mail_to)
    assert not any(isinstance(e, tuple) and e[0]=='login' for e in smtp.events)


def test_local_setup_hides_password_preserves_other_settings_and_does_not_send(settings, tmp_path, monkeypatch):
    from dotenv import dotenv_values

    from stock_daily.mailer import configure_gmail

    path = tmp_path / '.env'
    path.write_text('TUSHARE_TOKEN=keep-existing-token\nAPP_API_KEY=keep-management-key\n')
    answers = iter(['owner@gmail.com', ''])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'abcd efgh ijkl mnop')
    result = configure_gmail(settings, path)
    saved = dotenv_values(path)
    assert saved['SMTP_PASSWORD'] == 'abcdefghijklmnop'
    assert saved['MAIL_TO'] == saved['SMTP_USERNAME'] == 'owner@gmail.com'
    assert saved['TUSHARE_TOKEN'] == 'keep-existing-token'
    assert saved['APP_API_KEY'] == 'keep-management-key'
    assert saved['EMAIL_ENABLED'] == 'true'
    assert path.stat().st_mode & 0o777 == 0o600
    assert result['sent'] is False and saved['SMTP_PASSWORD'] not in str(result)
