import os
import re
import json
import sqlite3
import secrets
import csv
import io
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import resend
from fastapi import FastAPI, Request, BackgroundTasks, Header, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse

DB_PATH = os.environ.get("DB_PATH", "/app/data/cebec_compradores.db")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Angela Pelizer - CEBEC <contato@angelapelizer.com.br>")
LIVE_URL = os.environ.get("LIVE_URL", "https://angelapelizer.com.br/live")
WHATSAPP_GROUP_URL_DEFAULT = os.environ.get("WHATSAPP_GROUP_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MANYCHAT_API_KEY = os.environ.get("MANYCHAT_API_KEY", "")
MANYCHAT_BUYER_TAG = os.environ.get("MANYCHAT_BUYER_TAG", "comprou-live")
ACCEPTED_STATUSES = {"paid", "approved", "completed", "confirmed", "received"}

resend.api_key = RESEND_API_KEY

app = FastAPI(title="CEBEC Live Funnel")


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compradores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT UNIQUE NOT NULL,
                nome TEXT,
                email TEXT,
                telefone TEXT,
                valor INTEGER,
                data_compra TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"
        )
        if WHATSAPP_GROUP_URL_DEFAULT:
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES ('whatsapp_group_url', ?)",
                (WHATSAPP_GROUP_URL_DEFAULT,),
            )


@app.on_event("startup")
def on_startup():
    init_db()


def get_config(key: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def require_admin(authorization: str | None):
    if not ADMIN_TOKEN:
        raise HTTPException(500, "ADMIN_TOKEN não configurado no servidor")
    expected = f"Bearer {ADMIN_TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "Não autorizado")


def extract_lead(payload: dict) -> dict | None:
    """Aceita o payload normalizado do gancho asaas-webhook OU um payload
    tipo-Asaas cru (customer.name/email/phone|cellphone), tolerando variações."""

    # Formato normalizado (o que o gancho no asaas-webhook envia)
    if "transaction_id" in payload and "status" in payload:
        status = str(payload.get("status", "")).lower()
        if status not in ACCEPTED_STATUSES:
            return None
        return {
            "transaction_id": str(payload["transaction_id"]),
            "nome": payload.get("nome") or payload.get("name") or "",
            "email": payload.get("email") or "",
            "telefone": payload.get("telefone") or payload.get("phone") or "",
            "valor": int(payload.get("valor_cents") or payload.get("valor") or 0),
            "status": status,
        }

    # Formato cru tipo Asaas: {event, payment: {id, value, status, customer:{...}}}
    event = str(payload.get("event", "")).lower()
    resource = payload.get("payment") or payload.get("transaction") or payload
    status = str(resource.get("status", event)).lower()
    normalized_status = {
        "payment_confirmed": "confirmed",
        "payment_received": "received",
    }.get(status, status)
    if normalized_status not in ACCEPTED_STATUSES and not any(
        s in event for s in ACCEPTED_STATUSES
    ):
        return None

    customer = resource.get("customer") or {}
    tx_id = resource.get("id") or resource.get("transaction_id")
    if not tx_id:
        return None

    telefone = (
        customer.get("phone")
        or customer.get("cellphone")
        or customer.get("mobilePhone")
        or resource.get("customer_phone")
        or ""
    )
    email = customer.get("email") or resource.get("customer_email") or ""
    nome = customer.get("name") or resource.get("customer_name") or ""
    valor = resource.get("value") or resource.get("valor") or 0
    # Asaas manda valor em reais (float); normaliza pra centavos se vier fracionário
    valor_cents = int(round(valor * 100)) if isinstance(valor, float) else int(valor)

    return {
        "transaction_id": str(tx_id),
        "nome": nome,
        "email": email,
        "telefone": telefone,
        "valor": valor_cents,
        "status": normalized_status if normalized_status in ACCEPTED_STATUSES else "paid",
    }


def send_confirmation_email(nome: str, email: str):
    if not RESEND_API_KEY or not email:
        print(f"[email] skip — RESEND_API_KEY ausente ou email vazio (email={email!r})")
        return

    primeiro_nome = (nome or "").strip().split(" ")[0] or "tudo bem"
    redirect_url = "https://live.angelapelizer.com/cebec/entrar-grupo"

    html = f"""
    <div style="font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 560px; margin: 0 auto; color:#0B1929;">
      <div style="background:#0B1929; padding: 28px 32px; text-align:center;">
        <span style="color:#F5F4F0; font-size:13px; letter-spacing:0.18em; text-transform:uppercase;">CEBEC · Angela Pelizer</span>
      </div>
      <div style="padding: 32px; background:#F5F4F0;">
        <h1 style="font-size:22px; margin:0 0 16px;">Oi, {primeiro_nome}! Sua vaga está confirmada 🎉</h1>
        <p style="font-size:15px; line-height:1.6;">
          Você garantiu seu acesso à <strong>Live Fechada "Gestão de Impacto na Prática"</strong>,
          com Angela Pelizer.
        </p>
        <p style="font-size:15px; line-height:1.6;">
          📅 <strong>Segunda-feira, às 19h00</strong> — acesso exclusivo pra quem garantiu ingresso.
        </p>
        <div style="margin: 28px 0;">
          <a href="{redirect_url}"
             style="background:#3D7A45; color:#F5F4F0; text-decoration:none; padding:16px 32px;
                    border-radius:4px; font-weight:700; letter-spacing:0.04em; display:block;
                    text-align:center; margin-bottom:12px;">
            Entrar na comunidade
          </a>
          <a href="{LIVE_URL}"
             style="background:transparent; color:#3D7A45; text-decoration:none; padding:15px 31px;
                    border:1px solid #3D7A45; border-radius:4px; font-weight:700;
                    letter-spacing:0.04em; display:block; text-align:center;">
            Acessar a live
          </a>
        </div>
        <p style="font-size:14px; line-height:1.6; color:#333;">
          Guarde este e-mail: o link da live é o mesmo na segunda às 19h.
        </p>
        <p style="font-size:13px; color:#888; margin-top:32px;">
          Qualquer dúvida, é só responder este e-mail.
        </p>
      </div>
    </div>
    """

    try:
        resend.Emails.send(
            {
                "from": RESEND_FROM,
                "to": [email],
                "subject": "[Confirmado] Sua vaga na Live Fechada de Segunda - links de acesso",
                "html": html,
            }
        )
        print(f"[email] enviado pra {email}")
    except Exception as exc:
        print(f"[email] falhou pra {email}: {exc}")


def manychat_request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = f"https://api.manychat.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MANYCHAT_API_KEY}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def normalize_phone_br(telefone: str) -> str:
    digits = re.sub(r"\D", "", telefone or "")
    if not digits:
        return ""
    if not digits.startswith("55") and len(digits) in (10, 11):
        digits = "55" + digits
    return "+" + digits


def find_manychat_subscribers(field: str, value: str) -> list[int]:
    result = manychat_request("GET", "/fb/subscriber/findBySystemField", params={field: value})
    data = result.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [int(s["id"]) for s in data if s.get("id")]


def tag_manychat_buyer(email: str, telefone: str):
    if not MANYCHAT_API_KEY:
        print("[manychat] skip — MANYCHAT_API_KEY ausente")
        return
    try:
        ids = find_manychat_subscribers("email", email) if email else []
        phone = normalize_phone_br(telefone)
        if not ids and phone:
            ids = find_manychat_subscribers("phone", phone)
        if not ids:
            print(f"[manychat] comprador sem assinante no ManyChat (email={email!r})")
            return
        for sid in ids:
            manychat_request(
                "POST",
                "/fb/subscriber/addTagByName",
                body={"subscriber_id": sid, "tag_name": MANYCHAT_BUYER_TAG},
            )
            print(f"[manychat] tag {MANYCHAT_BUYER_TAG} aplicada em {sid}")
    except Exception as exc:
        print(f"[manychat] falhou (email={email!r}): {exc}")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cebec/entrar-grupo")
def entrar_grupo():
    url = get_config("whatsapp_group_url")
    if not url:
        raise HTTPException(404, "Link do grupo ainda não configurado")
    return RedirectResponse(url=url, status_code=302)


@app.post("/webhook/zpay")
async def webhook_zpay(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    lead = extract_lead(payload)
    if not lead:
        # evento não é pagamento aprovado — ack e ignora, sem retry storm
        return JSONResponse({"status": "ignored"}, status_code=200)

    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM compradores WHERE transaction_id = ?",
            (lead["transaction_id"],),
        ).fetchone()
        if existing:
            return JSONResponse({"status": "duplicated"}, status_code=200)

        conn.execute(
            """
            INSERT INTO compradores (transaction_id, nome, email, telefone, valor, data_compra, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead["transaction_id"],
                lead["nome"],
                lead["email"],
                lead["telefone"],
                lead["valor"],
                datetime.now(timezone.utc).isoformat(),
                lead["status"],
            ),
        )

    background_tasks.add_task(send_confirmation_email, lead["nome"], lead["email"])
    background_tasks.add_task(tag_manychat_buyer, lead["email"], lead["telefone"])

    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/admin/cebec/leads-semana")
def leads_semana(
    authorization: str | None = Header(default=None),
    format: str = Query(default="json"),
):
    require_admin(authorization)

    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT nome, telefone, email, data_compra, valor, status FROM compradores "
            "WHERE data_compra >= ? ORDER BY data_compra DESC",
            (since,),
        ).fetchall()

    leads = [dict(r) for r in rows]

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["nome", "whatsapp", "email", "data_compra", "valor_centavos", "status"])
        for l in leads:
            writer.writerow([l["nome"], l["telefone"], l["email"], l["data_compra"], l["valor"], l["status"]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")

    return {"count": len(leads), "leads": leads}


@app.post("/admin/cebec/config")
async def update_config(request: Request, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    body = await request.json()
    url = body.get("whatsapp_group_url")
    if not url:
        raise HTTPException(400, "whatsapp_group_url obrigatório")
    with db() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('whatsapp_group_url', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (url,),
        )
    return {"status": "ok", "whatsapp_group_url": url}
