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
    monkeypatch.delenv('DM_EXCLUDE_IG', raising=False)
    monkeypatch.setenv('UNSUB_SECRET', 'unsub-test')
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
    quiz(env)
    cb(env, 'cargo', cargo)
    return cb(env, 'porte', porte)


def quiz(env):
    for i, value in enumerate([2, 2, 1, 1, 1], 1):
        response = cb(env, f'q{i}', str(value))
    return response


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
    assert lead()['state'] == 'q1'
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
    assert response['messages'][0]['text'] == 'Obrigada! 🙏'
    assert 'buttons' not in response['messages'][0]
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
    quiz(env)
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


def test_quiz_score_result_and_repeat(env):
    send(env, 'MAPA')
    response = send(env, 'ana@example.org').json()['content']
    assert response['messages'][1]['text'].startswith('Pergunta 1 de 5')
    assert cb(env, 'q3', '2').json() == dm.EMPTY
    cb(env, 'q1', '0')
    cb(env, 'q1', '2')
    assert lead()['state'] == 'q2'
    assert lead()['q1'] == 2
    for i, value in [(2, 2), (3, 1), (4, 1), (5, 1)]:
        response = cb(env, f'q{i}', str(value)).json()['content']
    assert lead()['score'] == 7
    assert dm.tags('mapa-vermelho')[0] in response['actions']
    assert dm.field('score_mapa', 7) in response['actions']
    assert '7 de 10' in response['messages'][0]['text']
    mail = env[2].call_args.args[0]
    assert mail['subject'] == 'Seu resultado: 7 de 10 + as 5 perguntas em PDF'
    assert 'Baixar as 5 perguntas (PDF)' in mail['html']
    assert 'Não quero mais receber' in mail['html']
    cb(env, 'q5', '2')
    assert lead()['score'] == 8
    assert lead()['state'] == 'awaiting_cargo'
    assert env[2].call_count == 1
    token = main.unsubscribe_token('dm:123')
    assert env[0].get('/dm/descadastrar?s=123&t=bad').status_code == 400
    assert env[0].get('/dm/descadastrar', params={'s': '123', 't': token}).status_code == 200
    assert lead()['unsubscribed_at']


@pytest.mark.parametrize('hours,expected', [(2.99, 0), (3, 1), (22.99, 1), (23, 0)])
def test_quiz_abandonment(env, hours, expected):
    send(env, 'MAPA')
    send(env, 'ana@example.org')
    cb(env, 'q1', '2')
    asyncio.run(dm.worker_once(env[1] + timedelta(hours=hours)))
    assert env[3].call_count == expected
    if expected:
        content = env[3].call_args.kwargs['body']['data']['content']
        assert content['messages'][0]['text'] == 'Faltam só 4 perguntas pro seu resultado 👇'
        assert content['messages'][1]['text'].startswith('Pergunta 2 de 5')
        assert lead()['reminder_sent_at']
        quiz(env)
        cb(env, 'cargo', 'outro')
        cb(env, 'porte', 'ate50')
        asyncio.run(dm.worker_once(env[1] + timedelta(hours=22)))
        with main.db() as conn:
            assert conn.execute("SELECT status FROM dm_jobs WHERE kind='reminder'").fetchone()[0] == 'skipped'


@pytest.mark.parametrize('payload', [
    {'ig_username': 'ROGER.ARRUDA_'}, {'ig_username': 'jaopelizer'},
    {'tags': 'lead-mapa, nao-responder-bot'},
])
def test_exclusions_both_endpoints(env, payload):
    assert send(env, 'MAPA', **payload).json() == dm.EMPTY
    response = env[0].post('/dm/mapa/cb?step=q1&v=2', headers={'Authorization': 'Bearer secret'},
                           json={'subscriber_id': '123', **payload})
    assert response.json() == dm.EMPTY
    with main.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM dm_leads').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM dm_jobs').fetchone()[0] == 0
    assert not env[2].called and not env[3].called and not env[4].called


def test_configured_exclusion(env, monkeypatch):
    monkeypatch.setenv('DM_EXCLUDE_IG', ' Other , ANA ')
    assert send(env, 'MAPA').json() == dm.EMPTY


def full_contact(env, **overrides):
    payload = {'id': '123', 'key': 'user:123', 'page_id': 'page', 'status': 'active',
               'first_name': 'Ana', 'last_name': 'Silva', 'name': 'Ana Silva',
               'ig_username': 'ana.rh', 'ig_id': 123, 'phone': None,
               'last_input_text': 'MAPA', 'email': None,
               'tags': [{'id': 97229721, 'name': 'lead-mapa'}],
               'custom_fields': {'cargo': None, 'porte': None}, **overrides}
    return env[0].post('/dm/mapa', headers={'Authorization': 'Bearer secret'}, json=payload)


@pytest.mark.parametrize('sid', ['123', 123])
@pytest.mark.parametrize('configured', [False, True])
def test_full_contact_email(env, monkeypatch, sid, configured):
    if configured:
        monkeypatch.setenv('MANYCHAT_EMAIL_MAPA_FIELD_EXISTS', '1')
    response = full_contact(env, id=sid, email=' ANA@EMPRESA.COM.BR ')
    assert response.status_code == 200
    content = response.json()['content']
    assert content['messages'][-1]['text'].startswith('Pergunta 1 de 5') and len(content['messages']) == 2
    assert content['actions'] == dm.tags('lead-mapa') + (
        [dm.field('email_mapa', 'ana@empresa.com.br')] if configured else [])
    assert content['quick_replies'][0]['payload'] == {'subscriber_id': '123'}
    assert lead()['state'] == 'q1'
    assert lead()['email'] == 'ana@empresa.com.br'
    assert lead()['nome'] == 'Ana Silva'
    assert cb(env, 'q1', '2').status_code == 200
    assert lead()['state'] == 'q2'


@pytest.mark.parametrize('email', [None, '', 'bad', 'a@b.c'])
def test_full_contact_without_valid_email(env, email):
    response = full_contact(env, email=email, first_name=None, last_name=None,
                            ig_username=None, last_input_text=None, tags=None)
    assert response.status_code == 200
    assert 'Qual seu melhor e-mail?' in response.json()['content']['messages'][0]['text']
    assert lead()['state'] == 'awaiting_email'
    assert lead()['email'] is None
    assert lead()['nome'] == ''
    assert lead()['ig_username'] == ''


@pytest.mark.parametrize('tags', [' lead-mapa, lead-mapa ', ['lead-mapa'],
                                 [{'id': 1, 'name': 'lead-mapa'}]])
def test_full_contact_tags_empresa(env, tags):
    response = full_contact(env, last_input_text='EMPRESA', tags=tags)
    assert response.status_code == 200
    assert lead()['contact_accepted'] == 1
    assert env[2].call_count == 1


@pytest.mark.parametrize('payload', [
    {'tags': [{'id': 1, 'name': ' nao-responder-bot '}]},
    {'tags': ['lead-mapa', 'nao-responder-bot']},
    {'ig_username': '@ROGER.ARRUDA_'},
])
def test_full_contact_exclusions(env, payload):
    assert full_contact(env, email='ana@example.org', **payload).json() == dm.EMPTY
    response = env[0].post('/dm/mapa/cb?step=q1&v=2',
                           headers={'Authorization': 'Bearer secret'}, json={'id': 123, **payload})
    assert response.json() == dm.EMPTY
    with main.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM dm_leads').fetchone()[0] == 0
    assert not env[2].called and not env[3].called and not env[4].called


def test_full_contact_restart_and_legacy_precedence(env):
    send(env, 'MAPA')
    send(env, 'old@example.org')
    cb(env, 'q1', '2')
    response = full_contact(env, email='new@example.org', last_input_text=' mapa ')
    assert response.status_code == 200
    assert lead()['state'] == 'q1'
    assert lead()['q1'] is None and lead()['score'] is None
    assert lead()['email_attempts'] == 0
    assert lead()['email'] == 'new@example.org'
    response = full_contact(env, subscriber_id='123', id='other', text='obrigada',
                            email='ignored@example.org')
    assert response.json() == dm.EMPTY
    assert lead()['email'] == 'new@example.org'


def test_incoming_normalization():
    body = dm.Incoming(id=123, text=None, last_input_text=None,
                       tags=[{'name': ' lead-mapa '}, 'lead-mapa', {'name': None}])
    assert body.subscriber_id == '123'
    assert body.text == ''
    assert body.tags == {'lead-mapa'}
    with pytest.raises(ValueError):
        dm.Incoming(id=None)


@pytest.mark.parametrize('score,expected', [(0,'verde'), (3,'verde'), (4,'amarelo'), (6,'amarelo'), (7,'vermelho'), (10,'vermelho')])
def test_offer_bands(env, score, expected):
    deliver(env, 'gestor')
    with main.db() as conn:
        conn.execute('UPDATE dm_leads SET score=?', (score,))
    asyncio.run(dm.worker_once(env[1] + timedelta(minutes=2)))
    message = env[3].call_args.kwargs['body']['data']['content']['messages'][0]
    assert f'oferta-dm-{expected}' in message['buttons'][0]['url']
    assert (f'Com {score} de 10' if score >= 4 else 'Pra manter a equipe nesse nível') in message['text']
    response = env[0].get(message['buttons'][0]['url'], follow_redirects=False)
    assert response.status_code == 302
    assert f'utm_content=oferta-dm-{expected}' in response.headers['location']


def test_schema_migration_preserves_leads(env):
    send(env, 'MAPA')
    with main.db() as conn:
        for column in ['q1', 'q2', 'q3', 'q4', 'q5', 'score', 'unsubscribed_at']:
            conn.execute(f'ALTER TABLE dm_leads DROP COLUMN {column}')
    dm.init_db()
    dm.init_db()
    assert lead()['state'] == 'awaiting_email'
    assert lead()['score'] is None


def test_pdf_5_perguntas_served():
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app).get('/dm/5-perguntas.pdf')
    assert r.status_code == 200 and r.headers['content-type'] == 'application/pdf' and r.content[:4] == b'%PDF'


def test_dockerfile_copies_all_modules():
    import pathlib
    df = (pathlib.Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
    for mod in ['main.py', 'emails_abandono.py', 'email_layout.py', 'dm_mapa.py']:
        assert mod in df
    assert 'COPY static' in df
