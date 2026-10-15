import json
import smtplib
from dataclasses import replace
from datetime import timedelta
from email import policy
from email.parser import BytesParser

import pytest
from sqlalchemy import select
from test_mailer import mail_settings as mail_settings
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.backtest import simulate
from stock_daily.db import Database, mail_outbox, paper_accounts, trade_alerts
from stock_daily.mailer import flush_outbox, outbox_status
from stock_daily.paper_trading import execute_quotes
from stock_daily.providers import DataError
from stock_daily.trade_mail import queue_trade_alerts


def test_each_buy_and_sell_gets_frozen_alert_once_across_restart(db, mail_settings, monkeypatch):
    settings = replace(mail_settings, export_html=False, export_png=False)
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    events = db.rows(select(trade_alerts))
    assert len(events) == 2 and {e['payload']['side'] for e in events} == {'buy', 'sell'}
    assert queue_trade_alerts(db, settings)['queued'] == 2
    queued = db.rows(select(mail_outbox))
    frozen = {r['id']:r['message'] for r in queued}
    assert queue_trade_alerts(db, settings)['queued'] == 0
    assert execute_quotes(db, settings, quotes(), CURRENT)['status'] == 'unchanged'
    later = CURRENT + timedelta(minutes=10)
    execute_quotes(db, settings, quotes(later), later)
    assert len(db.rows(select(trade_alerts))) == 2
    sent = []
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda s, raw, recipient: sent.append((raw, recipient)))
    reopened = Database(settings.database_url)
    assert flush_outbox(reopened, settings, limit=10, slots=('trade',))['sent'] == 2
    assert flush_outbox(reopened, settings, limit=10)['sent'] == 0
    assert queue_trade_alerts(reopened, settings)['queued'] == 0
    for raw, recipient in sent:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        assert recipient == settings.mail_to
        assert '模拟成交' in str(message['Subject'])
        text = message.get_body(preferencelist=('plain',)).get_content()
        for label in ('模拟成交价', '成交后现金', '报价时间', '确认时间', '成交原因', '成交编号'):
            assert label in text
        assert '10:00:00' in text
        assert not list(message.iter_attachments())
        assert settings.smtp_password not in raw.decode()
        assert raw in frozen.values()
    assert all(r['status'] == 'sent' and r['attempts'] == 1 for r in reopened.rows(select(mail_outbox)))
    reopened.engine.dispose()


def test_alert_commit_and_account_commit_are_atomic(db, settings, monkeypatch):
    import stock_daily.trade_mail as trade_mail

    setup(db)
    before = simulate(db, settings, '20261014')
    original = trade_mail.record_alerts
    def failing(connection, settings, fills, current):
        original(connection, settings, fills, current)
        raise RuntimeError('simulated transaction failure')
    monkeypatch.setattr(trade_mail, 'record_alerts', failing)
    with pytest.raises(RuntimeError):
        execute_quotes(db, settings, quotes(), CURRENT)
    assert db.rows(select(paper_accounts)) == [] and db.rows(select(trade_alerts)) == []
    assert simulate(db, settings, '20261014') == before
    monkeypatch.setattr(trade_mail, 'record_alerts', original)
    assert execute_quotes(db, settings, quotes(), CURRENT)['new_fills'] == 2
    assert len(db.rows(select(trade_alerts))) == 2


def test_delivery_failure_does_not_rollback_trades_and_retry_is_durable(db, mail_settings, monkeypatch):
    setup(db)
    execute_quotes(db, mail_settings, quotes(), CURRENT)
    account = db.rows(select(paper_accounts))[0]
    queue_trade_alerts(db, mail_settings)
    clock = [CURRENT]
    monkeypatch.setattr('stock_daily.mailer.now', lambda: clock[0])
    # The queue clock is independent of the quote clock in this test.
    from sqlalchemy import update
    with db.engine.begin() as c:
        c.execute(update(mail_outbox).values(next_attempt_at=CURRENT.isoformat()))
    def fail(*args):
        raise smtplib.SMTPAuthenticationError(535, mail_settings.smtp_password.encode())
    monkeypatch.setattr('stock_daily.mailer.send_smtp', fail)
    assert flush_outbox(db, mail_settings, limit=10, slots=('trade',))['failed'] == 2
    assert db.rows(select(paper_accounts))[0] == account
    assert mail_settings.smtp_password not in json.dumps(outbox_status(db, mail_settings))
    assert flush_outbox(db, mail_settings, limit=10)['failed'] == 0
    clock[0] += timedelta(minutes=16)
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda *a: None)
    assert flush_outbox(db, mail_settings, limit=10)['sent'] == 2
    assert queue_trade_alerts(db, mail_settings)['queued'] == 0


def test_disabled_or_invalid_mail_preserves_events_without_fabricated_delivery(db, settings):
    setup(db)
    execute_quotes(db, settings, quotes(), CURRENT)
    assert queue_trade_alerts(db, settings)['status'] == 'disabled'
    assert len(db.rows(select(trade_alerts))) == 2
    with pytest.raises(DataError):
        queue_trade_alerts(db, replace(settings, email_enabled=True))
    assert db.rows(select(mail_outbox)) == []


def test_turning_trade_alerts_off_creates_no_notifications(db, settings):
    setup(db)
    execute_quotes(db, replace(settings, trade_email_enabled=False), quotes(), CURRENT)
    assert db.rows(select(trade_alerts)) == []


def test_worker_trade_mail_loop_recovers_events_without_running_quote_collection(db, mail_settings, monkeypatch):
    from stock_daily.scheduler import _trade_mail_loop

    setup(db)
    execute_quotes(db, mail_settings, quotes(), CURRENT)
    sent = []
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda s, raw, to: sent.append(raw))
    class StopAfterOne:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            assert seconds == 30
            self.stopped = True
    _trade_mail_loop(db, mail_settings, StopAfterOne())
    assert len(sent) == 2
    assert all(r['status'] == 'sent' for r in db.rows(select(mail_outbox)))


def test_disabling_trade_alerts_also_pauses_already_queued_trade_messages(db, mail_settings, monkeypatch):
    setup(db)
    execute_quotes(db, mail_settings, quotes(), CURRENT)
    queue_trade_alerts(db, mail_settings)
    sent = []
    monkeypatch.setattr('stock_daily.mailer.send_smtp', lambda *a: sent.append(1))
    assert flush_outbox(db, replace(mail_settings, trade_email_enabled=False), limit=10)['sent'] == 0
    assert not sent
