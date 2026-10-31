import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import main
import dm_mapa as dm


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'dm.db'))
    monkeypatch.setattr(main, 'ADMIN_TOKEN', 'admin')
    monkeypatch.setattr(main, 'RESEND_API_KEY', 'resend-test')
    monkeypatch.setattr(main, 'MANYCHAT_API_KEY', 'manychat-test')
    for key, value in {'DM_WEBHOOK_SECRET': 'secret', 'PDF_URL': 'https://example.org/pdf',
                       'B2B_ALERT_EMAILS': 'team@example.org', 'ZRM_LEAD_INGEST_URL': 'https://example.org/ingest',
                       'LEAD_INGEST_SECRET': 'ingest', 'ZRM_CEBEC_COMPANY_ID': 'company',
                       'ZRM_DM_CAMPAIGN_ID': 'campaign', 'PUBLIC_BASE_URL': 'https://example.org'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('MANYCHAT_EMAIL_MAPA_FIELD_EXISTS', raising=False)
    now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, 'now_utc', lambda: now)
    email = Mock(return_value={'id': 'mail'})
    manychat = Mock(return_value={'status': 'success'})
    post = Mock(return_value=Mock())
    monkeypatch.setattr(main.resend.Emails, 'send', email)
    monkeypatch.setattr(main, 'manychat_request', manychat)
    monkeypatch.setattr(dm.httpx, 'post', post)
    # Unexpected urllib traffic must fail, too.
    monkeypatch.setattr(main.urllib.request, 'urlopen', Mock(side_effect=AssertionError('network forbidden')))
    main.init_db()
    dm.init_db()
    with TestClient(main.app) as client:
        yield client, now, email, manychat, post


def send(env, text='', **kw):
    return env[0].post('/dm/mapa', headers={'Authorization': 'Bearer secret'},
                       json={'subscriber_id': '123', 'text': text, 'first_name': 'Ana', 'ig_username': 'ana', **kw})


def cb(env, step, value):
    return env[0].post(f'/dm/mapa/cb?step={step}&v={value}', headers={'Authorization': 'Bearer secret'},
                       json={'subscriber_id': '123'})


def lead():
    with main.db() as conn:
        return dict(conn.execute('SELECT * FROM dm_leads WHERE subscriber_id="123"').fetchone())


def deliver(env, cargo='rh', porte='200mais'):
    send(env, ' MAPA ')
    send(env, 'ana@example.org')
    cb(env, 'cargo', cargo)
    return cb(env, 'porte', porte)


def test_auth_optout_and_validation(env):
    assert env[0].post('/dm/mapa', json={'subscriber_id': '123'}).status_code == 401
    assert env[0].post('/dm/mapa/cb?step=cargo&v=rh', json={'subscriber_id': '123'}).status_code == 401
    assert send(env, 'MAPA', tags='lead-mapa, nao-responder-bot').json() == dm.EMPTY
    with main.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM dm_leads').fetchone()[0] == 0
    assert cb(env, 'cargo', 'invalid').status_code == 400
    assert cb(env, 'porte', 'ate50').json() == dm.EMPTY


def test_email_validation_and_custom_field(env, monkeypatch):
    response = send(env, 'mapa').json()
    assert response['content']['actions'] == dm.tags('lead-mapa')
    for value in ['bad', 'a@b.c']:
        assert 'digitar de novo' in send(env, value).json()['content']['messages'][0]['text']
    response = send(env, 'bad').json()
    assert lead()['state'] == 'awaiting_cargo'
    assert lead()['email'] is None
    assert all(len(r['caption']) <= 20 for r in response['content']['quick_replies'])
    send(env, 'MAPA')
    monkeypatch.setenv('MANYCHAT_EMAIL_MAPA_FIELD_EXISTS', '1')
    response = send(env, ' ANA@EXAMPLE.ORG ').json()
    assert response['content']['actions'] == [dm.field('email_mapa', 'ana@example.org')]
    assert response['content']['quick_replies'][0]['headers']['Authorization'] == 'Bearer secret'


def test_delivery_integrations_and_idempotency(env):
    response = deliver(env).json()['content']
    assert len(response['messages']) == 2
    assert response['messages'][0]['buttons'][0]['url'] == 'https://example.org/pdf'
    assert lead()['b2b'] == 1
    assert env[2].call_count == 2
    assert env[4].call_count == 1
    payload = env[4].call_args.kwargs['json']
    assert payload['source'] == 'Instagram DM'
    assert payload['campaign_id'] == 'campaign'
    assert payload['notes'] == 'cargo=rh; porte=200mais; ig=@ana'
    assert cb(env, 'porte', '200mais').json() == dm.EMPTY
    assert cb(env, 'cargo', 'rh').json() == dm.EMPTY
    assert send(env, 'obrigada').json() == dm.EMPTY
    assert env[2].call_count == 2
    with main.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM dm_jobs').fetchone()[0] == 5
    cb(env, 'b2b', 'sim')
    cb(env, 'b2b', 'sim')
    send(env, 'EMPRESA')
    assert env[2].call_count == 3
    assert 'aceitou contato' in env[2].call_args.args[0]['subject']


@pytest.mark.parametrize('cargo,porte,b2b', [('rh','ate50',0), ('dono','50a200',1), ('gestor','200mais',0), ('consultor','200mais',0)])
def test_qualification(env, cargo, porte, b2b):
    deliver(env, cargo, porte)
    assert lead()['b2b'] == b2b


def test_no_email_and_empresa(env):
    send(env, 'MAPA')
    for _ in range(3):
        send(env, 'bad')
    cb(env, 'cargo', 'outro')
    response = cb(env, 'porte', 'ate50').json()
    assert 'e-mail' not in response['content']['messages'][0]['text']
    assert env[2].call_count == 0
    send(env, 'empresa')
    send(env, 'EMPRESA')
    assert lead()['contact_accepted'] == 1
    assert env[2].call_count == 1


def test_offer_reminder_and_no_further_promos(env):
    deliver(env, 'gestor')
    asyncio.run(dm.worker_once(env[1] + timedelta(minutes=2)))
    assert env[3].call_count == 1
    data = env[3].call_args.kwargs['body']['data']
    assert data['content']['type'] == 'instagram'
    assert 'oferta-dm' in data['content']['messages'][0]['buttons'][0]['url']
    asyncio.run(dm.worker_once(env[1] + timedelta(hours=22)))
    assert env[3].call_count == 2
    assert lead()['reminder_sent_at']
    assert 'lembrete-dm' in env[3].call_args.kwargs['body']['data']['content']['messages'][0]['buttons'][0]['url']
    asyncio.run(dm.worker_once(env[1] + timedelta(days=2)))
    deliver(env, 'gestor')
    asyncio.run(dm.worker_once(env[1] + timedelta(hours=22)))
    assert env[3].call_count == 2


@pytest.mark.parametrize('reason', ['clicked', 'sent', 'expired'])
def test_reminder_skipped(env, reason):
    deliver(env, 'gestor')
    now = env[1] + timedelta(hours=22)
    if reason == 'clicked':
        response = env[0].get('/dm/r?s=123&to=ebook&utm_content=lembrete-dm', follow_redirects=False)
        assert response.status_code == 302
        assert 'utm_content=lembrete-dm' in response.headers['location']
        assert lead()['ebook_clicked_at']
    elif reason == 'sent':
        with main.db() as conn:
            conn.execute('UPDATE dm_leads SET reminder_sent_at=?', (env[1].isoformat(),))
    else:
        now = env[1] + timedelta(hours=23, minutes=50)
    asyncio.run(dm.worker_once(now))
    with main.db() as conn:
        assert conn.execute("SELECT status FROM dm_jobs WHERE kind='reminder'").fetchone()[0] == 'skipped'


def test_admin_and_redirect_validation(env):
    send(env, 'MAPA')
    assert env[0].get('/admin/dm/leads').status_code == 401
    headers = {'Authorization': 'Bearer admin'}
    assert env[0].get('/admin/dm/leads', headers=headers).json()['count'] == 1
    csv = env[0].get('/admin/dm/leads?format=csv', headers=headers)
    assert 'text/csv' in csv.headers['content-type']
    assert 'subscriber_id' in csv.text and 'Ana' in csv.text
    assert env[0].get('/dm/r?s=123&to=bad').status_code == 400


def test_failures_do_not_duplicate_or_block_other_jobs(env):
    env[2].side_effect = RuntimeError('sensitive provider response')
    deliver(env)
    assert env[4].call_count == 1
    asyncio.run(dm.worker_once())
    assert env[2].call_count == 2
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dm_jobs WHERE status='failed'").fetchone()[0] == 2
