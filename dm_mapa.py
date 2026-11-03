"""Funil Instagram MAPA, com estado e entregas persistidos em SQLite."""
import asyncio
from pathlib import Path
import csv
import html
import io
import logging
import os
import re
import secrets
import unicodedata
from datetime import timedelta, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

router = APIRouter()
log = logging.getLogger(__name__)
CARGOS = {'rh': 'RH', 'gestor': 'Gestor(a)/líder', 'dono': 'Dono(a)', 'consultor': 'Consultor/psicólogo', 'outro': 'Outro'}
PORTES = {'ate50': 'Até 50', '50a200': '50 a 200', '200mais': 'Mais de 200'}
EMPTY = {'version': 'v2', 'content': {'messages': []}}
QUESTIONS = [
    "Hora extra e 'tudo é urgente' viraram rotina na equipe?",
    'Tem gente que não sabe com clareza o que se espera dela ou quem decide o quê?',
    'O esforço das pessoas só é notado quando algo dá errado?',
    "Existem conflitos, ironias ou 'brincadeiras' que todo mundo vê e ninguém conversa?",
    'Em reunião, quase ninguém discorda ou traz um problema antes de virar crise?',
]
ANSWERS = {'0': 'Não', '1': 'Às vezes', '2': 'Sim, frequente'}
B2B = {'sim': 'Sim', 'nao': 'Agora não'}
CHOICES = {'cargo': CARGOS, 'porte': PORTES, 'b2b': B2B,
           **{f'q{i}': ANSWERS for i in range(1, 6)}}
PROMPTS = {
    'cargo': 'Pra eu te indicar o próximo passo certo: qual é o seu papel hoje?',
    'porte': 'E quantas pessoas trabalham na empresa?',
    'b2b': 'Vi que você cuida de uma empresa com bastante gente. Faz sentido a equipe do CEBEC te chamar aqui pra entender como vocês estão com a saúde da equipe?',
}
PLAN = ('Seu plano de 30 dias, um passo por semana:\n'
        '1. Escolha a pergunta que mais pesou e anote 3 situações reais.\n'
        '2. Converse com duas pessoas, sem julgamento.\n'
        '3. Teste uma mudança pequena por 7 dias.\n'
        '4. Revise o que mudou e ajuste.')


def excluded(body):
    usernames = {x.strip().lstrip('@').casefold() for x in os.environ.get(
        'DM_EXCLUDE_IG', 'roger.arruda_,jaopelizer').split(',') if x.strip()}
    return ('nao-responder-bot' in body.tags
            or body.ig_username.strip().lstrip('@').casefold() in usernames)


def band(score):
    return 'verde' if score <= 3 else 'amarelo' if score <= 6 else 'vermelho'


# Ordem de desempate da dor principal: o que mais adoece e mais vende o ebook primeiro.
PAIN_PRIORITY = ('q4', 'q5', 'q1', 'q2', 'q3')
PAIN_HOOKS = {
    'q1': 'hora extra e "tudo é urgente" virando rotina.',
    'q2': 'gente sem clareza do que se espera dela ou de quem decide.',
    'q3': 'esforço que só é notado quando algo dá errado.',
    'q4': 'conflito e "brincadeira" que todo mundo vê e ninguém conversa.',
    'q5': 'problema que só aparece quando já virou crise.',
}
PAIN_FIX = {  # só o que existe no ebook (ver CEBEC/low-ticket-lps/ebook/ebook-texto-extraido.txt)
    'q1': 'os 4 combinados mínimos de prioridade e papel e uma reunião de alinhamento de 15 minutos',
    'q2': 'um acordo de uma semana pra deixar claro quem entrega o quê, quem decide e quando vocês revisam',
    'q3': 'um inventário de contribuições e um jeito de reconhecer que vai além do "parabéns"',
    'q4': 'os 4 movimentos de uma conversa difícil que não vira briga',
    'q5': 'a escuta em 5 perguntas pra equipe trazer o problema antes da crise',
}


def top_pain(lead):
    """Pergunta com a resposta mais alta (2 antes de 1); empate pela PAIN_PRIORITY. None se tudo 0."""
    for level in (2, 1):
        for key in PAIN_PRIORITY:
            if lead.get(key) == level:
                return key
    return None


def result_text(score):
    descriptions = {
        'verde': '🟢 Atenção pontual\n\nBom ponto de partida. Converse com a equipe sobre o que pontuou antes que vire padrão.',
        'amarelo': '🟡 Sinal amarelo\n\nOs sinais já fazem parte da rotina. Vale escolher um deles e agir ainda este mês.',
        'vermelho': '🔴 Sinal vermelho\n\nO ambiente está desgastando as pessoas. É hora de levar o tema à liderança e ao RH.',
    }
    return f'Seu resultado: {score} de 10 ' + descriptions[band(score)]


def question(sid, number, prefix=None, actions=None):
    messages = ([{'type': 'text', 'text': prefix}] if prefix else [])
    messages.append({'type': 'text', 'text': f'Pergunta {number} de 5\n\n{QUESTIONS[number - 1]}'})
    return block('', actions, replies(sid, f'q{number}', ANSWERS), messages)


def core():
    import main
    return main


def base_url():
    return os.environ.get('PUBLIC_BASE_URL', 'https://live.angelapelizer.com').rstrip('/')


def init_db():
    with core().db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS dm_leads (
            subscriber_id TEXT PRIMARY KEY, state TEXT NOT NULL, email TEXT, cargo TEXT,
            porte TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            ebook_clicked_at TEXT, reminder_sent_at TEXT, b2b INTEGER NOT NULL DEFAULT 0,
            ig_username TEXT, nome TEXT, email_attempts INTEGER NOT NULL DEFAULT 0,
            last_user_at TEXT NOT NULL, contact_accepted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS dm_jobs (
            id INTEGER PRIMARY KEY, subscriber_id TEXT NOT NULL, kind TEXT NOT NULL,
            send_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(subscriber_id, kind)
        );
        CREATE INDEX IF NOT EXISTS dm_jobs_due ON dm_jobs(status, send_at);
        ''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(dm_leads)')}
        for name in ['q1', 'q2', 'q3', 'q4', 'q5', 'score']:
            if name not in columns:
                conn.execute(f'ALTER TABLE dm_leads ADD COLUMN {name} INTEGER')
        if 'unsubscribed_at' not in columns:
            conn.execute('ALTER TABLE dm_leads ADD COLUMN unsubscribed_at TEXT')


class Incoming(BaseModel):
    model_config = ConfigDict(extra='ignore')

    subscriber_id: str = Field(min_length=1, max_length=200)
    first_name: str = ''
    last_name: str = ''
    ig_username: str = ''
    text: str = ''
    email: str = ''
    tags: set[str] = Field(default_factory=set)

    @model_validator(mode='before')
    @classmethod
    def normalize_aliases(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            data['subscriber_id'] = data.get('subscriber_id') or data.get('id')
            data['text'] = data.get('text') or data.get('last_input_text') or ''
        return data

    @field_validator('subscriber_id', 'first_name', 'last_name', 'ig_username', 'text', 'email', mode='before')
    @classmethod
    def normalize_strings(cls, value):
        return '' if value is None else str(value)

    @field_validator('tags', mode='before')
    @classmethod
    def normalize_tags(cls, value):
        if value is None:
            return set()
        if isinstance(value, str):
            value = value.split(',')
        return {name.strip() for item in value
                if isinstance(name := item.get('name') if isinstance(item, dict) else item, str)
                and name.strip()}


def valid_email(email):
    return bool(re.fullmatch(r'[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)*\.[a-zA-Z]{2,}', email))


def authorize(authorization):
    secret = os.environ.get('DM_WEBHOOK_SECRET', '')
    if not secret or not authorization or not secrets.compare_digest(authorization, f'Bearer {secret}'):
        raise HTTPException(401, 'Não autorizado')


def tags(*names):
    return [{'action': 'add_tag', 'tag_name': name} for name in names]


def field(name, value):
    return {'action': 'set_field_value', 'field_name': name, 'value': value}


def block(text, actions=None, replies=None, messages=None):
    content = {'type': 'instagram', 'messages': messages or [{'type': 'text', 'text': text}]}
    if actions:
        content['actions'] = actions
    if replies:
        content['quick_replies'] = replies
    return {'version': 'v2', 'content': content}


def replies(sid, step, options):
    flow = os.environ.get('DM_REPLY_FLOW_NS')
    if flow:
        return [{'type': 'flow', 'caption': caption, 'target': flow}
                for caption in options.values()]
    return [{'type': 'dynamic_block_callback', 'caption': caption,
             'url': base_url() + '/dm/mapa/cb?' + urlencode({'step': step, 'v': value}),
             'method': 'post', 'headers': {'Authorization': 'Bearer ' + os.environ.get('DM_WEBHOOK_SECRET', '')},
             'payload': {'subscriber_id': sid}} for value, caption in options.items()]


def normalize_choice(text):
    text = unicodedata.normalize('NFKD', text.casefold())
    text = ''.join(c for c in text if not unicodedata.category(c).startswith(('M', 'S'))
                   and c != '\u200d')
    text = text.strip()
    while text and unicodedata.category(text[-1]).startswith('P'):
        text = text[:-1].rstrip()
    return ' '.join(text.split())


def match_choice(step, text):
    normalized = normalize_choice(text)
    for value, caption in CHOICES[step].items():
        if normalized in {normalize_choice(value), normalize_choice(caption)}:
            return value
    aliases = {}
    if step.startswith('q'):
        aliases = {'nao': '0', 'n': '0', 'as vezes': '1', 'talvez': '1',
                   'sim': '2', 's': '2', 'frequente': '2', 'sim frequente': '2'}
    elif step == 'b2b':
        aliases = {'sim': 'sim', 'quero': 'sim', 'pode': 'sim',
                   'nao': 'nao', 'agora nao': 'nao'}
    return aliases.get(normalized)


def retry_choice(sid, step):
    prefix = 'Não entendi 🙂 Toca numa das opções abaixo:'
    if step.startswith('q'):
        return question(sid, int(step[1]), prefix)
    return block('', replies=replies(sid, step, CHOICES[step]), messages=[
        {'type': 'text', 'text': prefix}, {'type': 'text', 'text': PROMPTS[step]}])


def enqueue(conn, sid, kind, when):
    conn.execute('INSERT OR IGNORE INTO dm_jobs(subscriber_id,kind,send_at) VALUES (?,?,?)',
                 (sid, kind, when.isoformat()))


def accepted(conn, sid, now):
    conn.execute('UPDATE dm_leads SET b2b=1,contact_accepted=1 WHERE subscriber_id=?', (sid,))
    enqueue(conn, sid, 'alert_accepted', now)


@router.post('/dm/mapa')
def mapa(body: Incoming, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None),
         entry: str = ''):
    authorize(authorization)
    incoming_tags = body.tags
    if excluded(body):
        return EMPTY
    sid, now = body.subscriber_id, core().now_utc()
    text = body.text.strip()
    with core().db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        lead = conn.execute('SELECT * FROM dm_leads WHERE subscriber_id=?', (sid,)).fetchone()
        if not lead:
            conn.execute('''INSERT INTO dm_leads(subscriber_id,state,created_at,updated_at,last_user_at,ig_username,nome)
                            VALUES (?,?,?,?,?,?,?)''',
                         (sid, 'awaiting_email', now.isoformat(), now.isoformat(), now.isoformat(), body.ig_username,
                          (body.first_name + ' ' + body.last_name).strip()))
        conn.execute('UPDATE dm_leads SET updated_at=?,last_user_at=? WHERE subscriber_id=?',
                     (now.isoformat(), now.isoformat(), sid))
        if text.upper() == 'EMPRESA' and (lead or 'lead-mapa' in incoming_tags):
            accepted(conn, sid, now)
            background_tasks.add_task(worker_once)
            return block('Obrigada! A equipe do CEBEC vai te chamar por aqui.', tags('qualificado-b2b', 'b2b-aceitou-contato'))
        if not lead or entry == '1' or text.upper() == 'MAPA':
            conn.execute("UPDATE dm_leads SET state='awaiting_email',email_attempts=0,q1=NULL,q2=NULL,q3=NULL,q4=NULL,q5=NULL,score=NULL WHERE subscriber_id=?", (sid,))
            email = body.email.strip().lower()
            if valid_email(email):
                conn.execute("UPDATE dm_leads SET state='q1',email=? WHERE subscriber_id=?", (email, sid))
                actions = tags('lead-mapa')
                if os.environ.get('MANYCHAT_EMAIL_MAPA_FIELD_EXISTS') == '1':
                    actions.append(field('email_mapa', email))
                return question(sid, 1, 'Anotado! Agora responde rapidinho, pensando nas últimas semanas (não num dia ruim). São 5 perguntas 👇', actions)
            return block('Oi! Aqui é da equipe da Dra. Angela 👋 Te mando agora as 5 perguntas pra descobrir se sua equipe está adoecendo. Qual seu melhor e-mail?\n\nUsamos seu e-mail só para enviar conteúdos do CEBEC. Você pode sair quando quiser.', tags('lead-mapa'))
        if lead['state'] != 'awaiting_email':
            step = {'awaiting_cargo': 'cargo', 'awaiting_porte': 'porte'}.get(
                lead['state'], lead['state'])
            if step == 'delivered' and lead['b2b'] and not lead['contact_accepted']:
                step = 'b2b'
            if step not in CHOICES:
                return EMPTY
            value = match_choice(step, text)
            if value is None:
                return retry_choice(sid, step)
            return handle_step(conn, lead, sid, step, value, now, background_tasks)
        email = text.lower()
        valid = valid_email(email)
        attempts = lead['email_attempts'] + 1
        if not valid and attempts < 3:
            conn.execute('UPDATE dm_leads SET email_attempts=? WHERE subscriber_id=?', (attempts, sid))
            return block('Hmm, acho que faltou algo no e-mail. Pode digitar de novo?')
        conn.execute("UPDATE dm_leads SET state='q1',email=?,email_attempts=? WHERE subscriber_id=?",
                     (email if valid else None, attempts, sid))
    # Explicit opt-in: only emit the custom field action when it has been configured.
    actions = [field('email_mapa', email)] if valid and os.environ.get('MANYCHAT_EMAIL_MAPA_FIELD_EXISTS') == '1' else []
    return question(sid, 1, 'Anotado! Agora responde rapidinho, pensando nas últimas semanas (não num dia ruim). São 5 perguntas 👇', actions)


@router.post('/dm/mapa/cb')
def callback(body: Incoming, step: str, v: str, background_tasks: BackgroundTasks,
             authorization: str | None = Header(default=None)):
    authorize(authorization)
    if excluded(body):
        return EMPTY
    choices = CHOICES
    if step not in choices or v not in choices[step]:
        raise HTTPException(400, 'Callback inválido')
    sid, now = body.subscriber_id, core().now_utc()
    with core().db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        lead = conn.execute('SELECT * FROM dm_leads WHERE subscriber_id=?', (sid,)).fetchone()
        if not lead:
            return EMPTY
        return handle_step(conn, lead, sid, step, v, now, background_tasks)


def handle_step(conn, lead, sid, step, v, now, background_tasks):
    choices = CHOICES
    if step in {f'q{i}' for i in range(1, 6)}:
        if lead['state'] != step and lead[step] is None:
            return EMPTY
        conn.execute(f'UPDATE dm_leads SET {step}=?,updated_at=?,last_user_at=? WHERE subscriber_id=?',
                     (int(v), now.isoformat(), now.isoformat(), sid))
        score = sum(int(v) if key == step else (lead[key] or 0) for key in choices if key.startswith('q'))
        conn.execute('UPDATE dm_leads SET score=? WHERE subscriber_id=?', (score, sid))
        if lead['state'] != step:
            return block('', [field('score_mapa', score)], messages=[{'type': 'text', 'text': 'Resposta atualizada.'}])
        number = int(step[1])
        if number < 5:
            conn.execute('UPDATE dm_leads SET state=? WHERE subscriber_id=?', (f'q{number + 1}', sid))
            return question(sid, number + 1)
        conn.execute("UPDATE dm_leads SET state='awaiting_cargo' WHERE subscriber_id=?", (sid,))
        enqueue(conn, sid, 'd0', now)
        background_tasks.add_task(worker_once)
        pdf = os.environ.get('PDF_URL', '').strip()
        plan = PLAN + (f'\n\n📄 Baixe aqui o material completo (PDF):\n{pdf}' if pdf else '') + (
            '\n\nTambém mandei no seu e-mail.' if lead['email'] else '')
        return block('', [field('score_mapa', score)] + tags('mapa-' + band(score)),
                     replies(sid, 'cargo', CARGOS), [
                         {'type': 'text', 'text': result_text(score)},
                         {'type': 'text', 'text': plan},
                         {'type': 'text', 'text': PROMPTS['cargo']}])
    if step == 'b2b':
        if lead['state'] != 'delivered' or not lead['b2b'] or lead['contact_accepted']:
            return EMPTY
    elif lead['state'] != 'awaiting_' + step:
        return EMPTY
    conn.execute('UPDATE dm_leads SET updated_at=?,last_user_at=? WHERE subscriber_id=?', (now.isoformat(), now.isoformat(), sid))
    if step == 'cargo':
        conn.execute("UPDATE dm_leads SET cargo=?,state='awaiting_porte' WHERE subscriber_id=?", (v, sid))
        return block(PROMPTS['porte'], [field('cargo', v)], replies(sid, 'porte', PORTES))
    if step == 'b2b':
        if v == 'nao':
            return block('Tudo bem! Qualquer coisa é só chamar.')
        accepted(conn, sid, now)
        background_tasks.add_task(worker_once)
        return block('Combinado! Alguém da equipe do CEBEC vai te chamar por aqui em breve.', tags('b2b-aceitou-contato'))
    b2b = lead['cargo'] in {'rh', 'dono'} and v in {'50a200', '200mais'}
    conn.execute("UPDATE dm_leads SET porte=?,b2b=?,state='delivered' WHERE subscriber_id=?", (v, b2b, sid))
    for kind, delay in [('zrm', timedelta()), ('offer', timedelta(minutes=2)), ('reminder', timedelta(hours=22))]:
        enqueue(conn, sid, kind, now + delay)
    if b2b:
        enqueue(conn, sid, 'alert_qualified', now)
    background_tasks.add_task(worker_once)
    messages = [{'type': 'text', 'text': 'Obrigada! 🙏'}]
    if b2b:
        messages.append({'type': 'text', 'text': PROMPTS['b2b']})
    return block('', [field('porte', v)] + (tags('qualificado-b2b') if b2b else []),
                 replies(sid, 'b2b', choices['b2b']) if b2b else None, messages)


def send_job(kind, lead):
    app = core()
    if kind == 'zrm':
        url, secret = os.environ.get('ZRM_LEAD_INGEST_URL'), os.environ.get('LEAD_INGEST_SECRET')
        company = os.environ.get('ZRM_CEBEC_COMPANY_ID')
        if not url or not secret or not company:
            raise RuntimeError('Configuração ZRM ausente')
        payload = {'company_id': company, 'name': lead['nome'], 'email': lead['email'],
                   'source': 'Instagram DM', 'entry_type': 'instagram_dm', 'utm_source': 'manychat',
                   'utm_medium': 'dm', 'utm_campaign': 'dm-mapa',
                   'notes': f"cargo={lead['cargo']}; porte={lead['porte']}; ig=@{(lead['ig_username'] or '').lstrip('@')}"}
        if os.environ.get('ZRM_DM_CAMPAIGN_ID'):
            payload['campaign_id'] = os.environ['ZRM_DM_CAMPAIGN_ID']
        response = httpx.post(url, headers={'Authorization': f'Bearer {secret}'}, json=payload, timeout=15)
        response.raise_for_status()
        return
    if kind in {'offer', 'reminder', 'quiz_reminder'}:
        if not app.MANYCHAT_API_KEY:
            raise RuntimeError('MANYCHAT_API_KEY ausente')
        score = lead['score'] or 0
        content = 'lembrete-dm' if kind == 'reminder' else 'oferta-dm-' + band(score)
        url = base_url() + '/dm/r?' + urlencode({'s': lead['subscriber_id'], 'to': 'ebook', 'utm_content': content})
        dor = top_pain(dict(lead))
        if kind == 'reminder':
            text = "Conseguiu responder as 5 perguntas? Se 3 ou mais deram 'sim', o ebook é o próximo passo mais rápido: R$ 9,90 👇"
        elif dor and score >= 4:
            text = (f'Com {score} de 10, o ponto que mais pesou foi: {PAIN_HOOKS[dor]}\n\n'
                    f'O ebook Liderança de Impacto tem {PAIN_FIX[dor]}, com exercício pra aplicar já nesta semana. R$ 9,90 👇')
        elif score >= 4:
            text = f'Com {score} de 10, o próximo passo é transformar isso em prática com a equipe. O ebook Liderança de Impacto traz 7 práticas com exercícios pra aplicar já nesta semana. R$ 9,90 👇'
        else:
            text = 'Pra manter a equipe nesse nível, o ebook Liderança de Impacto traz 7 práticas com exercícios pra aplicar no dia a dia. R$ 9,90 👇'
        data = block('', messages=[{'type': 'text', 'text': text, 'buttons': [{'type': 'url', 'caption': 'Quero o ebook', 'url': url}]}])
        if kind == 'quiz_reminder':
            number = int(lead['state'][1])
            data = question(lead['subscriber_id'], number, f'Faltam só {6 - number} perguntas pro seu resultado 👇')
        result = app.manychat_request('POST', '/fb/sending/sendContent', body={'subscriber_id': lead['subscriber_id'], 'data': data})
        if result.get('status') != 'success':
            raise RuntimeError('ManyChat recusou envio')
        return
    if kind == 'd0':
        if not lead['email'] or lead['unsubscribed_at']:
            return
        recipients = [lead['email']]
        subject = f"Seu resultado: {lead['score']} de 10 + as 5 perguntas em PDF"
        unsub = base_url() + '/dm/descadastrar?' + urlencode({'s': lead['subscriber_id'], 't': app.unsubscribe_token('dm:' + lead['subscriber_id'])})
        body = (f"<p>Olá, {html.escape(lead['nome'] or '')}!</p><p>" + result_text(lead['score']).replace('\n', '<br>') + '</p><p>' + PLAN.replace('\n', '<br>') + '</p>'
                + app.email_layout._button(html.escape(os.environ.get('PDF_URL', ''), quote=True), 'Baixar as 5 perguntas (PDF)')
                + app.email_layout._button(html.escape(base_url() + '/dm/r?' + urlencode({'s': lead['subscriber_id'], 'utm_content': 'oferta-dm-' + band(lead['score'])}), quote=True), 'Quero o ebook')
                + f'<p>Você está recebendo este e-mail porque respondeu ao MAPA do CEBEC.<br><a href="{html.escape(unsub, quote=True)}">Não quero mais receber estes e-mails</a></p>')
    else:
        recipients = [x.strip() for x in os.environ.get('B2B_ALERT_EMAILS', '').split(',') if x.strip()]
        if not recipients:
            raise RuntimeError('B2B_ALERT_EMAILS ausente')
        subject = 'CEBEC: ' + ('aceitou contato' if kind == 'alert_accepted' else 'lead qualificado B2B')
        body = '<br>'.join(html.escape(str(lead[key] or '')) for key in ('nome', 'ig_username', 'email', 'cargo', 'porte', 'subscriber_id'))
    if not app.RESEND_API_KEY:
        raise RuntimeError('RESEND_API_KEY ausente')
    app.resend.Emails.send({'from': app.RESEND_FROM, 'to': recipients, 'subject': subject, 'html': body})


async def worker_once(now=None):
    now = now or core().now_utc()
    with core().db() as conn:
        for lead in conn.execute("SELECT * FROM dm_leads WHERE state IN ('q1','q2','q3','q4','q5') AND reminder_sent_at IS NULL"):
            if now - datetime.fromisoformat(lead['last_user_at']) >= timedelta(hours=3):
                enqueue(conn, lead['subscriber_id'], 'quiz_reminder', now)
        ids = [r['id'] for r in conn.execute("SELECT id FROM dm_jobs WHERE status='pending' AND send_at<=? ORDER BY send_at,id", (now.isoformat(),))]
    for job_id in ids:
        with core().db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("UPDATE dm_jobs SET status='sending' WHERE id=? AND status='pending'", (job_id,)).rowcount != 1:
                continue
            job = conn.execute('SELECT * FROM dm_jobs WHERE id=?', (job_id,)).fetchone()
            lead = conn.execute('SELECT * FROM dm_leads WHERE subscriber_id=?', (job['subscriber_id'],)).fetchone()
            skip = not lead
            if lead and job['kind'] == 'quiz_reminder':
                elapsed = now - datetime.fromisoformat(lead['last_user_at'])
                skip = lead['state'] not in {'q1','q2','q3','q4','q5'} or bool(lead['reminder_sent_at']) or not timedelta(hours=3) <= elapsed < timedelta(hours=23)
            if lead and job['kind'] in {'offer', 'reminder'}:
                skip = now - datetime.fromisoformat(lead['last_user_at']) >= timedelta(hours=23, minutes=50)
                if job['kind'] == 'reminder':
                    skip = skip or bool(lead['ebook_clicked_at'] or lead['reminder_sent_at'])
            if skip:
                conn.execute("UPDATE dm_jobs SET status='skipped' WHERE id=?", (job_id,))
                continue
        try:
            await asyncio.to_thread(send_job, job['kind'], dict(lead))
            status = 'sent'
        except Exception:
            # Do not log provider exception bodies: they can contain personal data.
            log.warning('Falha no job DM id=%s tipo=%s', job_id, job['kind'])
            status = 'failed'
        with core().db() as conn:
            conn.execute('UPDATE dm_jobs SET status=? WHERE id=?', (status, job_id))
            if status == 'sent' and job['kind'] in {'reminder', 'quiz_reminder'}:
                conn.execute('UPDATE dm_leads SET reminder_sent_at=? WHERE subscriber_id=?', (now.isoformat(), lead['subscriber_id']))


async def worker_loop():
    while True:
        try:
            await worker_once()
        except Exception:
            log.warning('Falha no worker DM')
        await asyncio.sleep(60)


@router.on_event('startup')
async def startup():
    init_db()
    router.dm_worker = asyncio.create_task(worker_loop())


@router.on_event('shutdown')
async def shutdown():
    task = getattr(router, 'dm_worker', None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


PDF_FILE = Path(__file__).resolve().parent / 'static' / '5_Perguntas_Equipe_Adoecendo_CEBEC.pdf'


@router.get('/dm/5-perguntas.pdf')
def pdf_5_perguntas():
    if not PDF_FILE.exists():
        raise HTTPException(404)
    return FileResponse(PDF_FILE, media_type='application/pdf', filename=PDF_FILE.name)


@router.get('/dm/r')
def redirect(s: str, to: str = 'ebook', utm_content: str = 'oferta-dm'):
    if to != 'ebook' or utm_content not in {'oferta-dm', 'lembrete-dm', 'oferta-dm-verde', 'oferta-dm-amarelo', 'oferta-dm-vermelho'}:
        raise HTTPException(400, 'Destino inválido')
    with core().db() as conn:
        conn.execute('UPDATE dm_leads SET ebook_clicked_at=COALESCE(ebook_clicked_at,?) WHERE subscriber_id=?', (core().now_utc().isoformat(), s))
        lead = conn.execute('SELECT * FROM dm_leads WHERE subscriber_id=?', (s,)).fetchone()
    url = urlsplit(os.environ.get('EBOOK_URL', 'https://gestaodeimpacto.angelapelizer.com/ebook/'))
    query = dict(parse_qsl(url.query, keep_blank_values=True))
    query.update(utm_source='manychat', utm_medium='dm', utm_campaign='dm-mapa', utm_content=utm_content)
    if lead:
        dor = top_pain(dict(lead))
        if dor:
            query['dor'] = dor
            query['utm_term'] = 'dor-' + dor
        if lead['score'] is not None:
            query['faixa'] = band(lead['score'])
    return RedirectResponse(urlunsplit(url._replace(query=urlencode(query))), status_code=302)


@router.get('/dm/descadastrar')
@router.post('/dm/descadastrar')
def unsubscribe(s: str, t: str):
    if not os.environ.get('UNSUB_SECRET') or not secrets.compare_digest(t, core().unsubscribe_token('dm:' + s)):
        raise HTTPException(400, 'Token inválido')
    with core().db() as conn:
        conn.execute('UPDATE dm_leads SET unsubscribed_at=COALESCE(unsubscribed_at,?) WHERE subscriber_id=?',
                     (core().now_utc().isoformat(), s))
    return PlainTextResponse('Descadastro realizado.')


@router.get('/admin/dm/leads')
def admin(authorization: str | None = Header(default=None), format: str = 'json'):
    core().require_admin(authorization)
    with core().db() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM dm_leads ORDER BY created_at DESC')]
        columns = [r[1] for r in conn.execute('PRAGMA table_info(dm_leads)')]
    if format == 'csv':
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type='text/csv')
    return {'count': len(rows), 'leads': rows}
