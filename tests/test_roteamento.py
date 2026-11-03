import sqlite3
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import main
import email_layout

LIVE = '5130e417-e54e-4ea6-b665-9011cd1ae9f6'
LIVE2 = 'd8e43409-e35c-48f0-8fb7-43b7f72bf30d'
EBOOK = 'd6000597-5cf2-4cc9-8dc5-db34a1522111'
GI = '8748e80f-7cba-41f7-a2a7-65c84a2105b3'
BUMP = 'c8f0cbfc-14b4-4762-a4be-78507345824b'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'buyers.db'))
    monkeypatch.setattr(main, 'BONUS_PAYMENT_LINK_IDS', {'bonus'})
    monkeypatch.setattr(main, 'ADMIN_TOKEN', 'test-admin')
    monkeypatch.setattr(main, 'RESEND_API_KEY', 'test-only')
    monkeypatch.setattr(main, 'EBOOK_DOWNLOAD_URL', 'https://example.com/ebook')
    monkeypatch.delenv('VAGAS_RESTANTES_FIXO', raising=False)
    monkeypatch.delenv('TEST_EMAILS', raising=False)
    mail, tag = Mock(), Mock()
    monkeypatch.setattr(main.resend.Emails, 'send', mail)
    monkeypatch.setattr(main, 'tag_manychat_buyer', tag)
    main.init_db()
    # Sem startup: não iniciar worker nem tarefas de rede.
    with TestClient(main.app) as client:
        yield client, mail, tag


def payload(link, bumps=(), tx='purchase-1'):
    # Payload Vero normalizado pelo gancho, aceito por extract_lead.
    return dict(transaction_id=tx, status='paid', nome='Ana Silva',
                email=tx + '@example.com', telefone='11999999999',
                valor_cents=2900, payment_link_id=link,
                bump_ids=list(bumps), bump_amount_cents=2900 if bumps else 0)


@pytest.mark.parametrize('link,bumps,product,subjects,tagged', [
    (LIVE, [], 'live', ['[Confirmado]'], True),
    (LIVE2, [], 'live', ['[Confirmado]'], True),
    ('bonus', [], 'live', ['[Confirmado]'], True),
    (EBOOK, [], 'ebook', ['Seu ebook'], False),
    (EBOOK, [BUMP], 'ebook+live', ['[Confirmado]', 'Seu ebook'], True),
    (GI, [], 'gi', ['Boas-vindas'], False),
    ('unknown', [], 'outro', [], False),
    ('', [], 'outro', [], False),
    ('unknown', [BUMP], 'live', ['[Confirmado]'], True),
])
def test_routing_and_idempotency(setup, link, bumps, product, subjects, tagged):
    client, mail, tag = setup
    body = payload(link, bumps)
    assert client.post('/webhook/zpay', json=body).json() == {'status': 'ok'}
    assert client.post('/webhook/zpay', json=body).json() == {'status': 'duplicated'}
    with main.db() as conn:
        rows = conn.execute('SELECT * FROM compradores').fetchall()
        assert len(rows) == 1
        assert rows[0]['produto'] == product
        assert rows[0]['payment_link_id'] == link
    assert mail.call_count == len(subjects)
    for call, subject in zip(mail.call_args_list, subjects):
        assert subject in call.args[0]['subject']
    assert tag.call_count == int(tagged)


def test_missing_download_url(setup, monkeypatch, caplog):
    client, mail, tag = setup
    monkeypatch.setattr(main, 'EBOOK_DOWNLOAD_URL', '')
    client.post('/webhook/zpay', json=payload(EBOOK, [BUMP]))
    assert mail.call_count == 1  # confirmação da live continua
    assert tag.call_count == 1
    assert 'EBOOK_DOWNLOAD_URL' in caplog.text


def test_templates_and_bump_kit(setup, monkeypatch):
    client, mail, _ = setup
    monkeypatch.setattr(main, 'BUMP_KIT_URL', 'https://example.com/kit')
    client.post('/webhook/zpay', json=payload(EBOOK, [BUMP]))
    live_html, ebook_html = [call.args[0]['html'] for call in mail.call_args_list]
    assert 'Baixar o Kit' not in live_html
    assert 'Baixar o ebook' in ebook_html and 'https://example.com/ebook' in ebook_html
    assert 'Acessar a live' not in ebook_html
    assert main.addon_flags(EBOOK, [BUMP, 'disc']) == (False, True)
    assert main.addon_flags(EBOOK, [BUMP]) == (False, False)
    gi = email_layout.render_product_email('<script>', 'gi')
    assert 'em até 1 dia útil com seu acesso' in gi
    assert '<script>' not in gi and 'Acessar a live' not in gi
    assert 'Baixar o ebook' not in gi


def test_live_only_queries(setup):
    client, _, _ = setup
    for i, (link, bumps) in enumerate([(LIVE, []), (EBOOK, []), (EBOOK, [BUMP]), (GI, []), ('other', [])]):
        client.post('/webhook/zpay', json=payload(link, bumps, f'purchase-{i}'))
    data = client.get('/public/cebec/vagas').json()
    assert data['vendidas'] == 2 and len(data['recentes']) == 2
    headers = {'Authorization': 'Bearer test-admin'}
    admin = client.get('/admin/cebec/leads-semana', headers=headers).json()
    assert admin['count'] == 2
    assert {row['produto'] for row in admin['leads']} == {'live', 'ebook+live'}
    csv = client.get('/admin/cebec/leads-semana?format=csv', headers=headers).text
    assert 'produto' in csv.splitlines()[0] and 'ebook+live' in csv
    assert 'purchase-3@' not in csv
    with main.db() as conn:
        live, _ = main.target_live()
        assert not main.lead_has_bought(conn, 'purchase-1@example.com', '', live)
        assert main.lead_has_bought(conn, 'purchase-2@example.com', '', live)


def test_legacy_migration(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE compradores (id INTEGER PRIMARY KEY, transaction_id TEXT UNIQUE NOT NULL, nome TEXT, email TEXT, telefone TEXT, valor INTEGER, data_compra TEXT NOT NULL, status TEXT NOT NULL)')
        conn.execute("INSERT INTO compradores (transaction_id,data_compra,status) VALUES ('old','2026-10-21','paid')")
    monkeypatch.setattr(main, 'DB_PATH', str(path))
    main.init_db()
    main.init_db()
    with main.db() as conn:
        assert conn.execute('SELECT produto FROM compradores').fetchone()['produto'] == 'live'


def test_env_lists(monkeypatch):
    monkeypatch.setenv('ROUTING_TEST_IDS', ' one, two , ,three')
    assert main.env_ids('ROUTING_TEST_IDS', 'default') == {'one', 'two', 'three'}
    monkeypatch.setenv('ROUTING_TEST_IDS', '')
    assert main.env_ids('ROUTING_TEST_IDS', 'default') == set()
    monkeypatch.delenv('ROUTING_TEST_IDS')
    assert main.env_ids('ROUTING_TEST_IDS', 'default') == {'default'}
