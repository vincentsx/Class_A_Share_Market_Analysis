import json
import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.db import Database, mail_outbox, paper_accounts, trade_alerts, wecom_outbox
from stock_daily.operations import state
from stock_daily.paper_trading import execute_quotes
from stock_daily.wecom import (
    Client,
    ConnectionReplaced,
    ProtocolError,
    bind_callback,
    deliver_one,
    digest,
    prepare_pairing,
    queue_notifications,
    status_view,
    target,
)


@pytest.fixture
def wecom_settings(settings):
    return replace(settings, wecom_bot_id='bot-test', wecom_bot_secret='secret-private-sentinel',
        wecom_pair_code='pair-private-sentinel')


def callback(settings, userid='owner', code=None, chattype='single'):
    return {'cmd':'aibot_msg_callback','headers':{'req_id':'callback'},'body':{
        'aibotid':settings.wecom_bot_id,'msgtype':'text','msgid':'msg-1','chattype':chattype,
        'from':{'userid':userid},'chatid':'group-1','text':{'content':'订阅交易提醒 ' + (code or settings.wecom_pair_code)}}}


def test_binding_requires_one_use_unexpired_code_and_expected_bot_and_chat(wecom_settings, db, monkeypatch):
    s = wecom_settings
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    prepare_pairing(db,s)
    assert bind_callback(db,s,callback(s,code='bad-code')) is None
    assert bind_callback(db,s,callback(s,chattype='group')) is None
    foreign=callback(s)
    foreign['body']['aibotid']='other-bot'
    assert bind_callback(db,s,foreign) is None
    assert not target(db,s)
    assert bind_callback(db,s,callback(s))['chat_id']=='owner'
    assert state(db,s,'wecom_pairing')['used']
    assert bind_callback(db,s,callback(s,userid='intruder')) is None
    assert target(db,s)['chat_id']=='owner'
    assert not target(db,replace(s,wecom_bot_id='other-bot'))
    assert s.wecom_bot_secret not in repr(s) and s.wecom_pair_code not in repr(s)
    assert s.wecom_pair_code not in json.dumps(state(db,s,'wecom_pairing'))


def test_expired_pairing_is_not_automatically_reactivated(wecom_settings,db,monkeypatch):
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    prepare_pairing(db,wecom_settings)
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT+timedelta(minutes=31))
    prepare_pairing(db,wecom_settings)
    assert bind_callback(db,wecom_settings,callback(wecom_settings)) is None


def test_confirmed_trades_survive_without_email_and_notify_once_after_binding(wecom_settings,db,monkeypatch):
    s=replace(wecom_settings,trade_email_enabled=False)
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    setup(db)
    execute_quotes(db,s,quotes(),CURRENT)
    baseline=db.rows(select(paper_accounts))
    assert len(db.rows(select(trade_alerts)))==2 and not db.rows(select(mail_outbox))
    assert queue_notifications(db,s)==0
    prepare_pairing(db,s)
    assert bind_callback(db,s,callback(s))
    assert queue_notifications(db,s)==2 and queue_notifications(db,s)==0
    calls=[]
    class Sender:
        def send(self,*args):calls.append(args)
    assert deliver_one(db,s,Sender())
    reopened=Database(s.database_url)
    assert deliver_one(reopened,s,Sender())
    assert not deliver_one(reopened,s,Sender())
    assert queue_notifications(reopened,s)==0
    assert all(r['status']=='sent' and r['attempts']==1 for r in reopened.rows(select(wecom_outbox)))
    assert len(calls)==2 and all(c[0]=='owner' for c in calls)
    text='\n'.join(c[1] for c in calls)
    for label in ('模拟成交 · 买入','模拟成交 · 卖出','成交原因','成交后现金','报价时间','确认时间','成交编号'):
        assert label in text
    assert s.wecom_bot_secret not in text
    assert reopened.rows(select(paper_accounts))==baseline
    view=status_view(reopened,s)
    assert view['bound'] and view['counts']['sent']==2
    assert 'owner' not in json.dumps(view) and s.wecom_bot_secret not in json.dumps(view)
    reopened.engine.dispose()


def test_missing_either_credential_skips_wecom_and_does_not_reenable_disabled_email(settings,db):
    setup(db)
    s=replace(settings,trade_email_enabled=False,wecom_bot_id='id-only')
    execute_quotes(db,s,quotes(),CURRENT)
    assert not db.rows(select(trade_alerts)) and queue_notifications(db,s)==0
    assert not status_view(db,s)['enabled']


def test_failed_delivery_is_not_accepted_and_retries_have_stable_ids(wecom_settings,db,monkeypatch):
    s=replace(wecom_settings,wecom_chat_id='owner')
    clock=[CURRENT]
    monkeypatch.setattr('stock_daily.wecom.now',lambda:clock[0])
    setup(db)
    execute_quotes(db,s,quotes(),CURRENT)
    queue_notifications(db,s)
    ids=[]
    class Reject:
        def send(self,chat,text,req):
            ids.append(req)
            raise ProtocolError(45009)
    assert deliver_one(db,s,Reject())
    failed=db.rows(select(wecom_outbox).where(wecom_outbox.c.status=='failed'))[0]
    assert failed['attempts']==1 and '45009' in failed['last_error']
    class Accept:
        def send(self,chat,text,req):ids.append(req)
    deliver_one(db,s,Accept()) # the other trade can still be sent
    assert not deliver_one(db,s,Accept())
    clock[0]+=timedelta(minutes=2)
    assert deliver_one(db,s,Accept())
    assert ids[0]==ids[-1]
    assert all(r['status']=='sent' for r in db.rows(select(wecom_outbox)))


def test_crashed_sender_lease_restores_pending_message_without_mutating_trades(wecom_settings,db,monkeypatch):
    s=replace(wecom_settings,wecom_chat_id='owner')
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    setup(db)
    execute_quotes(db,s,quotes(),CURRENT)
    queue_notifications(db,s)
    first=db.rows(select(wecom_outbox))[0]
    with db.engine.begin() as c:
        c.execute(update(wecom_outbox).where(wecom_outbox.c.id==first['id']).values(
            status='sending',attempts=1,last_attempt_at=(CURRENT-timedelta(minutes=3)).isoformat()))
    class Accept:
        def send(self,*args):pass
    deliver_one(db,s,Accept())
    deliver_one(db,s,Accept())
    assert all(r['status']=='sent' for r in db.rows(select(wecom_outbox)))


def test_protocol_matches_receipt_ids_and_handles_callbacks_during_wait(wecom_settings,db):
    class Socket:
        def __init__(self):self.frames=[]
        def send(self,raw):self.frames.append(json.loads(raw))
        def recv(self,timeout):
            if len(self.frames)==1 and not getattr(self,'returned_other',False):
                self.returned_other=True
                return json.dumps({'headers':{'req_id':'unrelated'},'errcode':0})
            return json.dumps({'headers':{'req_id':self.frames[-1]['headers']['req_id']},'errcode':0})
    socket=Socket()
    client=Client(socket,db,wecom_settings,threading.Event())
    client.request('aibot_subscribe',{'bot_id':wecom_settings.wecom_bot_id,'secret':wecom_settings.wecom_bot_secret})
    client.send('owner','trade message','stable-id')
    assert socket.frames[1]=={'cmd':'aibot_send_msg','headers':{'req_id':'stable-id'},'body':{
        'chatid':'owner','msgtype':'markdown','markdown':{'content':'trade message'}}}
    with pytest.raises(ConnectionReplaced):
        client.callback({'cmd':'aibot_event_callback','body':{'event':{'eventtype':'disconnected_event'}}})


def test_changing_target_does_not_send_old_destination_queue(wecom_settings,db,monkeypatch):
    s=replace(wecom_settings,wecom_chat_id='owner')
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    setup(db)
    execute_quotes(db,s,quotes(),CURRENT)
    queue_notifications(db,s)
    changed=replace(s,wecom_chat_id='other-explicit-recipient')
    sent=[]
    class Accept:
        def send(self,*args):sent.append(args)
    assert not deliver_one(db,changed,Accept())
    assert not sent
    assert digest(s.wecom_bot_id+'|owner')!=digest(s.wecom_bot_id+'|other-explicit-recipient')


def test_timeout_does_not_mark_sent_or_leak_exception_details(wecom_settings,db,monkeypatch):
    s=replace(wecom_settings,wecom_chat_id='owner')
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    setup(db)
    execute_quotes(db,s,quotes(),CURRENT)
    queue_notifications(db,s)
    class Timeout:
        def send(self,*args):raise TimeoutError(s.wecom_bot_secret)
    with pytest.raises(TimeoutError):
        deliver_one(db,s,Timeout())
    view=status_view(db,s)
    assert view['counts']['failed']==1 and not view['counts'].get('sent')
    assert s.wecom_bot_secret not in json.dumps(view)
    assert all(r['last_error']!=s.wecom_bot_secret for r in db.rows(select(wecom_outbox)))


def test_local_retry_refuses_successful_or_other_destination_messages(wecom_settings,db,monkeypatch):
    from stock_daily.providers import DataError
    from stock_daily.wecom import retry_notification

    s=replace(wecom_settings,wecom_chat_id='owner')
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    setup(db)
    execute_quotes(db,s,quotes(),CURRENT)
    queue_notifications(db,s)
    class Reject:
        def send(self,*args):raise ProtocolError(45009)
    deliver_one(db,s,Reject())
    failed=db.rows(select(wecom_outbox).where(wecom_outbox.c.status=='failed'))[0]
    assert retry_notification(db,s,failed['id'])['status']=='pending'
    class Accept:
        def send(self,*args):pass
    while deliver_one(db,s,Accept()):
        pass
    with pytest.raises(DataError):
        retry_notification(db,s,failed['id'])
    assert all(r['status']=='sent' for r in db.rows(select(wecom_outbox)))


def test_new_pairing_cli_code_survives_worker_restart_with_old_environment(wecom_settings,db,monkeypatch):
    from stock_daily.wecom import create_pairing

    s=wecom_settings
    monkeypatch.setattr('stock_daily.wecom.now',lambda:CURRENT)
    prepare_pairing(db,s)
    command=create_pairing(db,s)['command']
    prepare_pairing(db,s)
    assert bind_callback(db,s,callback(s)) is None
    code=command.split()[-1]
    message=callback(s,code=code)
    message['body']['text']['content']='订阅交易提醒\n'+code
    assert bind_callback(db,s,message)['chat_id']=='owner'
    assert create_pairing(db,s)['status']=='bound'
