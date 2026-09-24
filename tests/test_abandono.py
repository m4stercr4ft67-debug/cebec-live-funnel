import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


def utc(y, m, d, hour, minute=0):
    return datetime(y, m, d, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    test_db = Path(__file__).with_name("abandono-test.db")
    test_db.unlink(missing_ok=True)
    monkeypatch.setattr(main, "DB_PATH", str(test_db))
    monkeypatch.setenv("LEAD_WEBHOOK_SECRET", "lead-secret")
    monkeypatch.setenv("UNSUB_SECRET", "unsub-secret")
    monkeypatch.setattr(main, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(main.resend.Emails, "send", lambda payload: {"id": "re_mock"})
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client
    test_db.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "instant,expected_day",
    [
        (utc(2026, 9, 28, 21, 29), 28),  # segunda 18:29 BRT
        (utc(2026, 9, 28, 21, 31), 5),   # segunda 18:31 BRT -> próxima
        (utc(2026, 9, 27, 15), 28),       # domingo
    ],
)
def test_target_live_and_closing(instant, expected_day):
    live, closing = main.target_live(instant)
    assert live.astimezone(main.BRT).day == expected_day
    assert live.astimezone(main.BRT).strftime("%H:%M") == "19:00"
    assert closing == live - timedelta(minutes=30)


def test_schedule_rules():
    live = utc(2026, 9, 28, 22)  # 19h BRT
    early = main.schedule_steps(utc(2026, 9, 25, 12), live)
    assert [s for s, (_, status) in early.items() if status == "pending"] == [1, 2, 3, 4, 5]
    late = main.schedule_steps(utc(2026, 9, 28, 19, 30), live)
    assert late[1][1] == "pending"
    assert late[2][1] == "skipped"
    assert late[3][1] == "skipped"
    assert late[4][1] == "skipped"
    assert late[5][1] == "skipped"  # já passou (16h BRT)


def post_lead(client, email="ana@example.com", whatsapp="11999999999"):
    return client.post(
        "/webhook/lead",
        headers={"Authorization": "Bearer lead-secret"},
        json={"nome": "Ana Silva", "email": email, "whatsapp": whatsapp},
    )


def test_webhook_lead_auth_validation_and_idempotency(client, monkeypatch):
    fixed = utc(2026, 9, 25, 12)
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    assert client.post("/webhook/lead", json={}).status_code == 401
    assert client.post("/webhook/lead", headers={"Authorization": "Bearer lead-secret"}, json={"email": "x"}).status_code == 400
    first, second = post_lead(client), post_lead(client)
    assert first.status_code == second.status_code == 200
    assert first.json()["scheduled"] == [1, 2, 3, 4, 5]
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM email_queue").fetchone()["n"] == 5


@pytest.mark.parametrize("buyer_email,buyer_phone", [("ANA@example.com", ""), ("other@example.com", "+55 (11) 99999-9999")])
def test_already_buyer_by_email_or_phone(client, monkeypatch, buyer_email, buyer_phone):
    fixed = utc(2026, 9, 25, 12)
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    with main.db() as conn:
        conn.execute("INSERT INTO compradores (transaction_id,nome,email,telefone,valor,data_compra,status) VALUES (?,?,?,?,?,?,?)", ("tx", "Ana", buyer_email, buyer_phone, 2900, fixed.isoformat(), "paid"))
    response = post_lead(client)
    assert response.json() == {"status": "already_buyer"}
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM email_queue").fetchone()["n"] == 0


def test_atomic_claim(client):
    with main.db() as conn:
        conn.execute("INSERT INTO leads (email,live_at,created_at) VALUES (?,?,?)", ("a@b.com", utc(2026, 9, 28, 22).isoformat(), utc(2026, 9, 25, 12).isoformat()))
        lead_id = conn.execute("SELECT id FROM leads").fetchone()["id"]
        conn.execute("INSERT INTO email_queue (lead_id,step,send_at) VALUES (?,?,?)", (lead_id, 1, utc(2026, 9, 25, 13).isoformat()))
        queue_id = conn.execute("SELECT id FROM email_queue").fetchone()["id"]
    assert main.claim_email(queue_id) is True
    assert main.claim_email(queue_id) is False


def test_checkout_before_after_and_discount_rule(client, monkeypatch):
    monday_before = utc(2026, 9, 28, 21, 29)
    monkeypatch.setattr(main, "now_utc", lambda: monday_before)
    with main.db() as conn:
        conn.execute("INSERT INTO leads (email,live_at,created_at) VALUES (?,?,?)", ("a@b.com", utc(2026, 9, 28, 22).isoformat(), monday_before.isoformat()))
        lead_id = conn.execute("SELECT id FROM leads").fetchone()["id"]
        conn.execute("INSERT INTO email_queue (lead_id,step,send_at,status) VALUES (?,?,?,?)", (lead_id, 5, monday_before.isoformat(), "pending"))
    normal = client.get(f"/cebec/checkout?o=1990&e=5&l={lead_id}", follow_redirects=False)
    assert normal.headers["location"] == main.CHECKOUT_URL_STD_DEFAULT
    with main.db() as conn:
        conn.execute("UPDATE email_queue SET status='sent' WHERE lead_id=? AND step=5", (lead_id,))
    discount = client.get(f"/cebec/checkout?o=1990&e=5&l={lead_id}", follow_redirects=False)
    assert discount.headers["location"] == main.CHECKOUT_URL_1990_DEFAULT
    monkeypatch.setattr(main, "now_utc", lambda: utc(2026, 9, 28, 21, 31))
    closed = client.get("/cebec/checkout?o=std", follow_redirects=False)
    assert closed.headers["location"].endswith("?encerrado=1")


def test_public_vagas_has_no_pii_and_filters_tests(client, monkeypatch):
    fixed = utc(2026, 9, 27, 15)
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    monkeypatch.setenv("TEST_EMAILS", "ignore@example.com")
    with main.db() as conn:
        values = [
            ("real-1", "maria souza", "maria@example.com", "5511999999999"),
            ("test-1", "Teste", "test@example.com", ""),
            ("real-2", "Ignorar", "ignore@example.com", ""),
        ]
        for tx, name, email, phone in values:
            conn.execute("INSERT INTO compradores (transaction_id,nome,email,telefone,valor,data_compra,status) VALUES (?,?,?,?,?,?,?)", (tx, name, email, phone, 2900, fixed.isoformat(), "paid"))
    response = client.get("/public/cebec/vagas")
    data = response.json()
    assert data["vendidas"] == 1
    assert data["recentes"] == [{"nome": "Maria", "ha_min": 0}]
    assert "maria@example.com" not in response.text and "5511" not in response.text
    assert response.headers["cache-control"] == "public, max-age=30"


def test_unsubscribe_valid_and_invalid(client):
    with main.db() as conn:
        conn.execute("INSERT INTO leads (email,live_at,created_at) VALUES (?,?,?)", ("a@b.com", utc(2026, 9, 28, 22).isoformat(), utc(2026, 9, 25, 12).isoformat()))
        lead_id = conn.execute("SELECT id FROM leads").fetchone()["id"]
        conn.execute("INSERT INTO email_queue (lead_id,step,send_at) VALUES (?,?,?)", (lead_id, 1, utc(2026, 9, 25, 13).isoformat()))
    assert client.get(f"/cebec/descadastrar?l={lead_id}&t=bad").status_code == 400
    token = main.unsubscribe_token(lead_id)
    response = client.post(f"/cebec/descadastrar?l={lead_id}&t={token}")
    assert response.status_code == 200
    with main.db() as conn:
        assert conn.execute("SELECT unsubscribed_at FROM leads WHERE id=?", (lead_id,)).fetchone()["unsubscribed_at"]
        assert conn.execute("SELECT status FROM email_queue").fetchone()["status"] == "skipped"


def test_worker_renders_and_sends_with_mock(client, monkeypatch):
    fixed = utc(2026, 9, 25, 12)
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    sent = []
    monkeypatch.setattr(main.resend.Emails, "send", lambda payload: sent.append(payload) or {"id": "re_123"})
    with main.db() as conn:
        conn.execute("INSERT INTO leads (email,nome,live_at,created_at) VALUES (?,?,?,?)", ("a@b.com", "Ana", utc(2026, 9, 28, 22).isoformat(), fixed.isoformat()))
        lead_id = conn.execute("SELECT id FROM leads").fetchone()["id"]
        conn.execute("INSERT INTO email_queue (lead_id,step,send_at) VALUES (?,?,?)", (lead_id, 1, (fixed - timedelta(minutes=1)).isoformat()))
    asyncio.run(main.email_worker_once(fixed))
    assert sent[0]["tags"][0] == {"name": "seq", "value": "abandono"}
    assert "List-Unsubscribe" in sent[0]["headers"]
    with main.db() as conn:
        row = conn.execute("SELECT status,resend_id FROM email_queue").fetchone()
        assert (row["status"], row["resend_id"]) == ("sent", "re_123")
