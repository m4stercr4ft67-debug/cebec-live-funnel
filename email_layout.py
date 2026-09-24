"""Layout HTML dos e-mails da sequência de recuperação.

Estrutura em tabelas com estilos inline (compatível com Gmail, Outlook e Apple Mail).
Imagens hospedadas na LP (Netlify), sempre com texto alternativo.
"""

ASSETS = "https://gestaodeimpacto.angelapelizer.com/img/email"
LOGO = f"{ASSETS}/logo-cebec-branco.png"
AVATAR = f"{ASSETS}/angela-avatar.jpg"
BANNER = f"{ASSETS}/angela-banner.jpg"

NAVY = "#0B1929"
GREEN = "#3D7A45"
AMBER = "#D9B061"
PAPER = "#F5F4F0"
TEXT = "#1C2833"
MUTED = "#5B6670"

SANS = "Helvetica, Arial, sans-serif"
SERIF = "Georgia, 'Times New Roman', serif"

# Passos com foto grande da Angela no topo
BANNER_STEPS = {1, 4}
# Passos com a faixa "quando / como"
INFO_STEPS = {1, 2, 4}


def quote_block(text: str, author: str) -> str:
    """Depoimento em cartão (usado dentro dos parágrafos da copy)."""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 14px">'
        f'<tr><td style="background:#FFFFFF;border:1px solid #E4E1D8;border-radius:10px;padding:18px 20px">'
        f'<div style="font-family:{SERIF};font-size:17px;line-height:1.5;font-style:italic;color:{TEXT}">&ldquo;{text}&rdquo;</div>'
        f'<div style="font-family:{SANS};font-size:12px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:{GREEN};margin-top:10px">{author}</div>'
        f'</td></tr></table>'
    )


def _button(url: str, label: str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 22px">'
        f'<tr><td align="center" bgcolor="{GREEN}" style="border-radius:8px">'
        f'<a href="{url}" target="_blank" style="display:block;padding:17px 24px;font-family:{SANS};font-size:17px;font-weight:bold;'
        f'color:#FFFFFF;text-decoration:none;border-radius:8px">{label} &rarr;</a>'
        f'</td></tr></table>'
    )


def _info_strip(data_live: str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 22px">'
        f'<tr><td style="background:{PAPER};border-radius:10px;padding:14px 18px;font-family:{SANS};font-size:14px;line-height:1.55;color:{TEXT}">'
        f'<strong style="color:{NAVY}">{data_live[:1].upper() + data_live[1:]}, às 19h</strong> (Brasília)<br>'
        f'Ao vivo, online &middot; gravação disponível por 30 dias'
        f'</td></tr></table>'
    )


def _signature() -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px">'
        f'<tr><td valign="middle" style="padding-right:14px">'
        f'<img src="{AVATAR}" width="64" height="64" alt="Angela Pelizer" style="display:block;border-radius:50%;border:2px solid {AMBER}">'
        f'</td><td valign="middle" style="font-family:{SANS};font-size:14px;line-height:1.4;color:{TEXT}">'
        f'<strong style="font-family:{SERIF};font-size:17px;color:{NAVY}">Angela Pelizer</strong><br>'
        f'<span style="color:{MUTED}">Fundadora do CEBEC</span>'
        f'</td></tr></table>'
    )


def render(*, step, preheader, paragraphs, cta_label, cta_url, after_cta, data_live, unsubscribe_url, open_pixel_url):
    """Monta o HTML final. `paragraphs` e `after_cta` já vêm formatados (com nome escapado)."""
    body = "".join(
        f'<div style="font-family:{SANS};font-size:16px;line-height:1.65;color:{TEXT};margin:0 0 16px">{p}</div>'
        for p in paragraphs
    )
    closing_lines = after_cta
    closing = "".join(
        f'<div style="font-family:{SANS};font-size:15px;line-height:1.6;color:{TEXT};margin:0 0 10px">{p}</div>'
        for p in closing_lines
    )
    banner = (
        f'<tr><td style="padding:0"><img src="{BANNER}" width="600" alt="Angela Pelizer" '
        f'style="display:block;width:100%;max-width:600px;height:auto;border:0"></td></tr>'
        if step in BANNER_STEPS else ""
    )
    info = _info_strip(data_live) if step in INFO_STEPS else ""

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>CEBEC · Angela Pelizer</title></head>
<body style="margin:0;padding:0;background:#E9E7E1">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#E9E7E1">
<tr><td align="center" style="padding:24px 12px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#FFFFFF;border-radius:14px;overflow:hidden">
    <tr><td align="center" style="background:{NAVY};padding:26px 24px 22px">
      <img src="{LOGO}" width="170" alt="CEBEC · Centro Educacional de Bem-Estar Corporativo" style="display:block;width:170px;height:auto;border:0">
      <div style="font-family:{SANS};font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:{AMBER};margin-top:12px">Live · Gestão de Impacto na Prática</div>
    </td></tr>
    {banner}
    <tr><td style="padding:34px 32px 30px">
      {body}
      {info}
      {_button(cta_url, cta_label)}
      {closing}
      <div style="height:10px;line-height:10px">&nbsp;</div>
      {_signature()}
    </td></tr>
    <tr><td style="background:{PAPER};padding:20px 32px;font-family:{SANS};font-size:12px;line-height:1.6;color:{MUTED}">
      Você está recebendo este e-mail porque iniciou sua inscrição na live Gestão de Impacto na Prática.<br>
      <a href="https://instagram.com/angela.pelizer" style="color:{GREEN};text-decoration:none">@angela.pelizer</a> &middot;
      <a href="{unsubscribe_url}" style="color:{MUTED}">Não quero mais receber estes e-mails</a>
    </td></tr>
  </table>
</td></tr>
</table>
<img src="{open_pixel_url}" width="1" height="1" alt="" style="display:block;width:1px;height:1px;border:0">
</body></html>"""
