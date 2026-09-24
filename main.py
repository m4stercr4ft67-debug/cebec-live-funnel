import os
import re
import json
import sqlite3
import secrets
import csv
import io
import urllib.request
import urllib.parse
import asyncio
import hashlib
import hmac
import html as html_lib
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import resend
from fastapi import FastAPI, Request, BackgroundTasks, Header, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from emails_abandono import SEQUENCIA

DB_PATH = os.environ.get("DB_PATH", "/app/data/cebec_compradores.db")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Angela Pelizer - CEBEC <contato@angelapelizer.com.br>")
LIVE_URL = os.environ.get("LIVE_URL", "https://angelapelizer.com.br/live")
WHATSAPP_GROUP_URL_DEFAULT = os.environ.get("WHATSAPP_GROUP_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MANYCHAT_API_KEY = os.environ.get("MANYCHAT_API_KEY", "")
MANYCHAT_BUYER_TAG = os.environ.get("MANYCHAT_BUYER_TAG", "comprou-live")
ACCEPTED_STATUSES = {"paid", "approved", "completed", "confirmed", "received"}
BRT = ZoneInfo("America/Sao_Paulo")
CHECKOUT_URL_STD_DEFAULT = "https://pay.ogrupozentra.com/checkout/link/5130e417-e54e-4ea6-b665-9011cd1ae9f6"
CHECKOUT_URL_1990_DEFAULT = "https://pay.ogrupozentra.com/checkout/link/5441d11b-5764-48f9-95de-42b1fbc7a113"

resend.api_key = RESEND_API_KEY

app = FastAPI(title="CEBEC Live Funnel")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://gestaodeimpacto.angelapelizer.com"]
    + (["http://127.0.0.1:8765"] if os.environ.get("DEV_CORS") == "1" else []),
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, nome TEXT,
                telefone TEXT, utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
                utm_content TEXT, utm_term TEXT, live_at TEXT NOT NULL,
                created_at TEXT NOT NULL, unsubscribed_at TEXT, UNIQUE(email, live_at)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL,
                step INTEGER NOT NULL, send_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', sent_at TEXT,
                resend_id TEXT, error TEXT, UNIQUE(lead_id, step),
                FOREIGN KEY(lead_id) REFERENCES leads(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER,
                step INTEGER, offer TEXT, clicked_at TEXT NOT NULL
            )
        """)
        if WHATSAPP_GROUP_URL_DEFAULT:
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES ('whatsapp_group_url', ?)",
                (WHATSAPP_GROUP_URL_DEFAULT,),
            )


@app.on_event("startup")
async def on_startup():
    init_db()
    app.state.email_worker = asyncio.create_task(email_worker_loop())


@app.on_event("shutdown")
async def on_shutdown():
    task = getattr(app.state, "email_worker", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def target_live(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Retorna a próxima live cuja janela de inscrição ainda está aberta."""
    local = (now or now_utc()).astimezone(BRT)
    days = (7 - local.weekday()) % 7
    live = (local + timedelta(days=days)).replace(hour=19, minute=0, second=0, microsecond=0)
    closing = live - timedelta(minutes=30)
    if local >= closing:
        live += timedelta(days=7)
        closing += timedelta(days=7)
    return live, closing


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def normalize_buyer_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits and not digits.startswith("55") and len(digits) in (10, 11):
        digits = "55" + digits
    return digits


def lead_has_bought(conn: sqlite3.Connection, email: str, telefone: str, live_at: datetime) -> bool:
    since = iso_utc(live_at - timedelta(days=7))
    phone = normalize_buyer_phone(telefone)
    rows = conn.execute(
        "SELECT email, telefone FROM compradores WHERE data_compra >= ?", (since,)
    ).fetchall()
    email = (email or "").strip().lower()
    return any(
        ((r["email"] or "").strip().lower() == email)
        or (phone and normalize_buyer_phone(r["telefone"] or "") == phone)
        for r in rows
    )


def schedule_steps(now: datetime, live: datetime) -> dict[int, tuple[datetime, str]]:
    """Calcula os cinco horários e aplica as regras de espaçamento da sequência."""
    now, live = now.astimezone(timezone.utc), live.astimezone(timezone.utc)
    closing = live - timedelta(minutes=30)
    times = {
        1: now + timedelta(minutes=40),
        2: now + timedelta(hours=20),
        3: live - timedelta(hours=24),
        4: live - timedelta(hours=9),
        5: live - timedelta(hours=3),
    }
    result: dict[int, tuple[datetime, str]] = {}
    previous: datetime | None = None
    for step, when in times.items():
        valid = when > now and when < closing
        if step == 2 and when > times[3] - timedelta(hours=3):
            valid = False
        if step != 5 and valid and previous and when < previous + timedelta(hours=3):
            valid = False
        status = "pending" if valid else "skipped"
        result[step] = (when, status)
        if valid:
            previous = when
    return result


def unsubscribe_token(lead_id: int) -> str:
    secret = os.environ.get("UNSUB_SECRET", "")
    return hmac.new(secret.encode(), str(lead_id).encode(), hashlib.sha256).hexdigest()[:32]


def get_week_sales(conn: sqlite3.Connection, live: datetime, include_recent: bool = False):
    start, end = iso_utc(live - timedelta(days=7)), iso_utc(live)
    excluded = {x.strip().lower() for x in os.environ.get("TEST_EMAILS", "").split(",") if x.strip()}
    rows = conn.execute(
        "SELECT nome, email, data_compra, transaction_id FROM compradores "
        "WHERE data_compra > ? AND data_compra <= ? ORDER BY data_compra DESC", (start, end)
    ).fetchall()
    clean, seen = [], set()
    for row in rows:
        email = (row["email"] or "").strip().lower()
        if email in excluded or re.match(r"(?i)^teste?", row["transaction_id"] or "") or email in seen:
            continue
        seen.add(email)
        clean.append(row)
    if not include_recent:
        return len(clean)
    now = now_utc()
    recent = []
    for row in clean:
        first = (row["nome"] or "").strip().split(" ")[0].capitalize()
        if first:
            try:
                bought = datetime.fromisoformat(row["data_compra"])
                if bought.tzinfo is None:
                    bought = bought.replace(tzinfo=timezone.utc)
                recent.append({"nome": first, "ha_min": max(0, int((now - bought).total_seconds() // 60))})
            except ValueError:
                continue
        if len(recent) == 8:
            break
    return len(clean), recent


def render_abandonment_email(lead: sqlite3.Row, step: int, remaining: int) -> tuple[str, str, str, str]:
    template = SEQUENCIA[step - 1]
    first = (lead["nome"] or "").strip().split(" ")[0] or "Olá"
    live = datetime.fromisoformat(lead["live_at"]).astimezone(BRT)
    months = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    weekdays = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    data_live = f"{weekdays[live.weekday()]}, {live.day} de {months[live.month - 1]}"
    offer = "1990" if step == 5 else "std"
    checkout = f"https://live.angelapelizer.com/cebec/checkout?o={offer}&e={step}&l={lead['id']}&utm_source=email&utm_medium=recuperacao&utm_campaign=live-cebec&utm_content=e{step}"
    unsub = f"https://live.angelapelizer.com/cebec/descadastrar?l={lead['id']}&t={unsubscribe_token(lead['id'])}"
    values = dict(primeiro_nome=first, checkout_url=checkout, lp_url="https://gestaodeimpacto.angelapelizer.com/?utm_source=email&utm_medium=recuperacao&utm_campaign=live-cebec", data_live=data_live, vagas_restantes=remaining, unsubscribe_url=unsub)
    subject = template["subject"].format(**values)
    html_values = dict(values, primeiro_nome=html_lib.escape(first))
    preheader = template["preheader"].format(**html_values)
    paragraphs = [p.format(**html_values) for p in template["paragraphs"]]
    after = [p.format(**html_values) for p in template["after_cta"]]
    blocks = "".join(f'<div style="font-size:15px;line-height:1.6;margin:0 0 16px">{p}</div>' for p in paragraphs)
    after_html = "".join(f'<div style="font-size:14px;line-height:1.6;margin:0 0 10px">{p}</div>' for p in after)
    body = f'''<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;color:#0B1929"><span style="display:none!important;visibility:hidden;opacity:0;height:0;width:0">{preheader}</span><div style="background:#0B1929;padding:28px 32px;text-align:center"><span style="color:#F5F4F0;font-size:13px;letter-spacing:.18em;text-transform:uppercase">CEBEC · Angela Pelizer</span></div><div style="padding:32px;background:#F5F4F0">{blocks}<div style="margin:28px 0"><a href="{checkout}" style="background:#3D7A45;color:#F5F4F0;text-decoration:none;padding:16px 32px;border-radius:4px;font-weight:700;display:block;text-align:center">{template['cta']}</a></div>{after_html}<p style="font-size:11px;color:#777;margin-top:32px">Não quer mais receber estes e-mails? <a href="{unsub}">Descadastre-se</a>.</p></div></div>'''
    plain_parts = [re.sub(r"<[^>]+>", "", p).replace("&nbsp;", " ") for p in paragraphs + after]
    text_version = html_lib.unescape("\n\n".join(plain_parts) + f"\n\n{template['cta']}: {checkout}\n\nDescadastrar: {unsub}")
    return subject, body, text_version, unsub


def claim_email(queue_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("UPDATE email_queue SET status='sending' WHERE id=? AND status='pending'", (queue_id,))
        return cur.rowcount == 1


def skip_remaining(conn: sqlite3.Connection, lead_id: int):
    conn.execute("UPDATE email_queue SET status='skipped' WHERE lead_id=? AND status IN ('pending','sending')", (lead_id,))


async def process_email(queue_id: int):
    if not claim_email(queue_id):
        return
    with db() as conn:
        row = conn.execute("SELECT q.step, l.* FROM email_queue q JOIN leads l ON l.id=q.lead_id WHERE q.id=?", (queue_id,)).fetchone()
        if not row:
            return
        live = datetime.fromisoformat(row["live_at"])
        if row["unsubscribed_at"] or lead_has_bought(conn, row["email"], row["telefone"], live):
            skip_remaining(conn, row["id"])
            return
        total = int(os.environ.get("VAGAS_SEMANA", "40"))
        remaining = max(0, total - get_week_sales(conn, live))
        subject, html, text_version, unsub = render_abandonment_email(row, row["step"], remaining)
    payload = {"from": RESEND_FROM, "to": [row["email"]], "subject": subject, "html": html, "text": text_version, "headers": {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}, "tags": [{"name": "seq", "value": "abandono"}, {"name": "step", "value": f"e{row['step']}"}]}
    last_error = None
    for attempt in range(2):
        try:
            response = await asyncio.to_thread(resend.Emails.send, payload)
            resend_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
            with db() as conn:
                conn.execute("UPDATE email_queue SET status='sent', sent_at=?, resend_id=?, error=NULL WHERE id=?", (iso_utc(now_utc()), resend_id, queue_id))
            return
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(300)
    with db() as conn:
        conn.execute("UPDATE email_queue SET status='failed', error=? WHERE id=?", (str(last_error)[:1000], queue_id))


async def email_worker_once(now: datetime | None = None):
    cutoff = iso_utc(now or now_utc())
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM email_queue WHERE status='pending' AND send_at<=? ORDER BY send_at", (cutoff,)).fetchall()]
    for queue_id in ids:
        await process_email(queue_id)


async def email_worker_loop():
    while True:
        try:
            await email_worker_once()
        except Exception as exc:
            print(f"[abandono-worker] erro: {exc}")
        await asyncio.sleep(60)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/lead")
async def webhook_lead(request: Request, authorization: str | None = Header(default=None)):
    secret = os.environ.get("LEAD_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(500, "LEAD_WEBHOOK_SECRET não configurado no servidor")
    expected = f"Bearer {secret}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "Não autorizado")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")
    email = str(body.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(400, "E-mail inválido")
    now = now_utc()
    live, _ = target_live(now)
    live_iso = iso_utc(live)
    with db() as conn:
        conn.execute(
            """INSERT INTO leads (email,nome,telefone,utm_source,utm_medium,utm_campaign,utm_content,utm_term,live_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(email,live_at) DO UPDATE SET
               nome=excluded.nome, telefone=excluded.telefone, utm_source=excluded.utm_source,
               utm_medium=excluded.utm_medium, utm_campaign=excluded.utm_campaign,
               utm_content=excluded.utm_content, utm_term=excluded.utm_term""",
            (email, body.get("nome") or "", body.get("whatsapp") or "", body.get("utm_source"), body.get("utm_medium"), body.get("utm_campaign"), body.get("utm_content"), body.get("utm_term"), live_iso, iso_utc(now)),
        )
        lead = conn.execute("SELECT * FROM leads WHERE email=? AND live_at=?", (email, live_iso)).fetchone()
        if lead_has_bought(conn, email, lead["telefone"], live):
            skip_remaining(conn, lead["id"])
            return {"status": "already_buyer"}
        schedule = schedule_steps(now, live)
        for step, (send_at, status) in schedule.items():
            conn.execute("INSERT OR IGNORE INTO email_queue (lead_id,step,send_at,status) VALUES (?,?,?,?)", (lead["id"], step, iso_utc(send_at), status))
        scheduled = [step for step, (_, status) in schedule.items() if status == "pending"]
    return {"status": "ok", "live_at": live_iso, "scheduled": scheduled}


def checkout_is_open(now: datetime) -> bool:
    local = now.astimezone(BRT)
    if local.weekday() == 0 and (local.hour, local.minute) >= (18, 30):
        return False
    return True


@app.get("/cebec/checkout")
def checkout(o: str = Query(default="std"), e: int | None = Query(default=None), l: int | None = Query(default=None)):
    now = now_utc()
    if not checkout_is_open(now):
        url = "https://gestaodeimpacto.angelapelizer.com/?encerrado=1"
    else:
        offer = "std"
        if o == "1990" and l is not None:
            with db() as conn:
                sent = conn.execute("SELECT 1 FROM email_queue WHERE lead_id=? AND step=5 AND status='sent'", (l,)).fetchone()
            if sent:
                offer = "1990"
        url = os.environ.get("CHECKOUT_URL_1990", CHECKOUT_URL_1990_DEFAULT) if offer == "1990" else os.environ.get("CHECKOUT_URL_STD", CHECKOUT_URL_STD_DEFAULT)
    if l is not None:
        with db() as conn:
            if conn.execute("SELECT 1 FROM leads WHERE id=?", (l,)).fetchone():
                conn.execute("INSERT INTO email_clicks (lead_id,step,offer,clicked_at) VALUES (?,?,?,?)", (l, e, o, iso_utc(now)))
    return RedirectResponse(url, status_code=302)


@app.get("/public/cebec/vagas")
def public_vagas():
    live, closing = target_live()
    total = max(0, int(os.environ.get("VAGAS_SEMANA", "40")))
    with db() as conn:
        sold, recent = get_week_sales(conn, live, include_recent=True)
    return JSONResponse({"total": total, "vendidas": sold, "restantes": max(0, total - sold), "live_at": iso_utc(live), "fechamento": iso_utc(closing), "recentes": recent}, headers={"Cache-Control": "public, max-age=30"})


def unsubscribe_lead(lead_id: int, token: str):
    if not os.environ.get("UNSUB_SECRET") or not secrets.compare_digest(token, unsubscribe_token(lead_id)):
        raise HTTPException(400, "Token inválido")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            raise HTTPException(400, "Token inválido")
        conn.execute("UPDATE leads SET unsubscribed_at=COALESCE(unsubscribed_at,?) WHERE id=?", (iso_utc(now_utc()), lead_id))
        skip_remaining(conn, lead_id)


@app.get("/cebec/descadastrar")
def unsubscribe_get(l: int, t: str):
    unsubscribe_lead(l, t)
    return HTMLResponse("<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Descadastro</title><body><h1>Você não receberá mais e-mails sobre a live.</h1></body></html>")


@app.post("/cebec/descadastrar")
def unsubscribe_post(l: int, t: str):
    unsubscribe_lead(l, t)
    return {"status": "ok"}


@app.get("/admin/cebec/abandono")
def abandonment_admin(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    with db() as conn:
        rows = conn.execute("""SELECT q.step,
            SUM(CASE WHEN q.status IN ('pending','sending') THEN 1 ELSE 0 END) agendados,
            SUM(CASE WHEN q.status='sent' THEN 1 ELSE 0 END) enviados,
            SUM(CASE WHEN q.status='skipped' THEN 1 ELSE 0 END) pulados,
            SUM(CASE WHEN q.status='failed' THEN 1 ELSE 0 END) falhas,
            (SELECT COUNT(*) FROM email_clicks c WHERE c.step=q.step) cliques
            FROM email_queue q GROUP BY q.step ORDER BY q.step""").fetchall()
        lead_count = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        converted = 0
        for lead in conn.execute("SELECT * FROM leads").fetchall():
            if lead_has_bought(conn, lead["email"], lead["telefone"], datetime.fromisoformat(lead["live_at"])):
                converted += 1
    by_step = {f"e{r['step']}": {k: (r[k] or 0) for k in ("agendados", "enviados", "pulados", "falhas", "cliques")} for r in rows}
    return {"leads": lead_count, "compradores": converted, "por_passo": by_step}


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
