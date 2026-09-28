"""Painel mobile do agente de prospecção do Saulo.

Roda como um site simples (funciona bem no navegador do celular) e expõe um
endpoint /tick que executa UM ciclo de trabalho (buscar -> rascunhar -> enviar
e-mails aprovados). Um serviço externo grátis (cron-job.org) chama /tick a
cada 10-15 min, 24 horas por dia — é isso que mantém o agente "sempre ativo"
mesmo em hospedagem gratuita que dorme quando ninguém acessa.
"""
import functools, html, os, sqlite3, threading, time
from datetime import datetime

from flask import Flask, redirect, request, url_for

import core

app = Flask(__name__)
LOG = []  # últimas linhas de atividade, em memória
LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        LOG.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
        del LOG[:-200]
    print(msg, flush=True)


def need_auth(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        pw = os.environ.get("APP_PASSWORD")
        if pw and request.authorization != None and request.authorization.password == pw:
            return fn(*a, **kw)
        if pw:
            return ("Senha necessária", 401, {"WWW-Authenticate": 'Basic realm="prospector"'})
        return fn(*a, **kw)
    return wrap


def page(body, title="Agente de Prospecção"):
    nav = " · ".join(f'<a href="{u}">{t}</a>' for u, t in
                     (("/", "Início"), ("/buscas", "Buscas"), ("/revisar", "Revisar"),
                      ("/leads", "Leads"), ("/bloqueio", "Bloqueio")))
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root{{color-scheme:light dark}}
body{{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:0 auto;
     padding:12px 14px 60px;line-height:1.45}}
nav{{position:sticky;top:0;background:Canvas;padding:10px 0;border-bottom:1px solid #8884;
    font-size:14px;margin-bottom:14px}}
nav a{{margin-right:4px;text-decoration:none}}
.card{{border:1px solid #8884;border-radius:10px;padding:12px;margin-bottom:12px}}
textarea{{width:100%;min-height:110px;font:inherit;box-sizing:border-box}}
input[type=text],input[type=tel],select{{width:100%;padding:8px;font:inherit;
    box-sizing:border-box;margin-bottom:8px}}
button,.btn{{padding:9px 14px;border-radius:8px;border:1px solid #8886;background:#8881;
    font:inherit;margin:3px 4px 3px 0;cursor:pointer;display:inline-block;text-decoration:none;color:inherit}}
.ok{{border-color:#2a8}} .bad{{border-color:#c44}} .muted{{opacity:.65;font-size:13px}}
form{{display:inline}}
pre{{white-space:pre-wrap;font-family:inherit;background:#8881;padding:10px;border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td,th{{padding:6px 4px;border-bottom:1px solid #8883;text-align:left}}
</style></head><body><nav>{nav}</nav>{body}</body></html>"""


def esc(s):
    return html.escape(s or "")


# --------------------------------------------------------------------- home
@app.route("/")
@need_auth
def home():
    con = core.db()
    counts = dict(con.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status").fetchall())
    pend = con.execute("SELECT COUNT(*) c FROM messages WHERE status='rascunho'").fetchone()["c"]
    aprov = con.execute("SELECT COUNT(*) c FROM messages WHERE status='aprovada'").fetchone()["c"]
    enviados = con.execute("SELECT COUNT(*) c FROM messages WHERE status='enviada'").fetchone()["c"]
    cards = "".join(f'<div class="card"><b>{esc(k)}</b>: {v}</div>' for k, v in counts.items()) or \
            '<p class="muted">Nenhum lead ainda — cadastre uma busca na aba Buscas.</p>'
    with LOG_LOCK:
        logtxt = "\n".join(LOG[-40:]) or "(sem atividade ainda)"
    body = f"""
    <h2>Painel</h2>
    <div class="card">Rascunhos p/ revisar: <b>{pend}</b> ·
      Aprovadas na fila: <b>{aprov}</b> · Enviadas: <b>{enviados}</b></div>
    {cards}
    <form method="post" action="/tick-manual"><button>Rodar um ciclo agora</button></form>
    <h3>Atividade recente</h3><pre>{esc(logtxt)}</pre>
    <p class="muted">Endpoint automático: <code>/tick?key=SEU_TICK_SECRET</code> —
    configure um pinger grátis (ex.: cron-job.org) para chamar essa URL a cada 10-15 min,
    24h por dia. Veja o README para o passo a passo.</p>
    """
    return page(body)

CYCLE_LOCK = threading.Lock()

def start_cycle():
    """Roda um ciclo em segundo plano. Devolve False se já houver um em andamento."""
    if not CYCLE_LOCK.acquire(blocking=False):
        return False
    def work():
        try:
            core.run_cycle(log)
        except Exception as e:
            log(f"ciclo falhou: {e}")
        finally:
            CYCLE_LOCK.release()
    threading.Thread(target=work, daemon=True).start()
    return True

@app.route("/tick-manual", methods=["POST"])
@need_auth
def tick_manual():
    if not start_cycle():
        log("já existe um ciclo em andamento")
    return redirect(url_for("home"))

@app.route("/tick")
def tick():
    """Chamado pelo serviço externo de ping. Não exige login, mas exige a chave.
    Responde na hora e faz o trabalho em segundo plano (evita timeout do servidor)."""
    secret = os.environ.get("TICK_SECRET")
    if not secret or request.args.get("key") != secret:
        return "chave inválida", 403
    return "ciclo iniciado" if start_cycle() else "ciclo anterior ainda rodando"


# ------------------------------------------------------------------- buscas
CATS_HELP = ", ".join(sorted(core.CATS))

@app.route("/buscas", methods=["GET", "POST"])
@need_auth
def buscas():
    con = core.db()
    if request.method == "POST":
        cat, city = request.form.get("category", "").strip(), request.form.get("city", "").strip()
        if cat and city:
            con.execute("INSERT INTO queries(category, city) VALUES(?,?)", (cat, city))
            con.commit()
        return redirect(url_for("buscas"))
    rows = con.execute("SELECT * FROM queries ORDER BY id DESC").fetchall()
    items = "".join(
        f'<div class="card">{esc(q["category"])} — {esc(q["city"])} '
        f'<span class="muted">(última busca: {q["last_run"] or "nunca"})</span><br>'
        f'<form method="post" action="/buscas/{q["id"]}/toggle">'
        f'<button class="{"bad" if q["active"] else "ok"}">{"Pausar" if q["active"] else "Reativar"}</button>'
        f"</form></div>" for q in rows) or '<p class="muted">Nenhuma busca cadastrada.</p>'
    body = f"""<h2>Buscas ativas</h2>
    <p class="muted">A cada ciclo o agente roda a busca mais "esquecida" da lista.
    Categorias reconhecidas: {esc(CATS_HELP)}. Também aceita chave=valor do OpenStreetMap
    (ex.: shop=bicycle).</p>
    <form method="post" class="card">
      <input type="text" name="category" placeholder="categoria (ex.: barbearia)" required>
      <input type="text" name="city" placeholder="cidade ou bairro (ex.: Copacabana, Rio de Janeiro)" required>
      <button>Adicionar busca</button>
    </form>
    {items}"""
    return page(body)

@app.route("/buscas/<int:qid>/toggle", methods=["POST"])
@need_auth
def toggle_busca(qid):
    con = core.db()
    con.execute("UPDATE queries SET active = 1 - active WHERE id=?", (qid,))
    con.commit()
    return redirect(url_for("buscas"))


# ------------------------------------------------------------------ revisar
@app.route("/revisar")
@need_auth
def revisar():
    con = core.db()
    rows = con.execute("SELECT m.*, l.name, l.address FROM messages m JOIN leads l ON l.id=m.lead_id "
                       "WHERE m.status='rascunho' ORDER BY m.id LIMIT 25").fetchall()
    if not rows:
        return page("<h2>Revisar</h2><p class='muted'>Nada pendente agora.</p>")
    items = []
    for m in rows:
        assunto = f"<b>Assunto:</b> {esc(m['subject'])}<br>" if m["subject"] else ""
        items.append(f"""<div class="card">
          <b>{esc(m['name'])}</b> · {esc(m['channel'])} · <span class="muted">{esc(m['target'])}</span><br>
          <span class="muted">{esc(m['address'] or '')}</span>
          <form method="post" action="/revisar/{m['id']}">
            {assunto}
            <textarea name="body">{esc(m['body'])}</textarea>
            <button name="acao" value="aprovar" class="ok">Aprovar</button>
            <button name="acao" value="rejeitar" class="bad">Rejeitar</button>
          </form></div>""")
    return page("<h2>Revisar rascunhos</h2>" + "".join(items))

@app.route("/revisar/<int:mid>", methods=["POST"])
@need_auth
def revisar_um(mid):
    con = core.db()
    acao, body = request.form.get("acao"), request.form.get("body", "")
    if acao == "aprovar":
        con.execute("UPDATE messages SET body=?, status='aprovada' WHERE id=?", (body, mid))
    elif acao == "rejeitar":
        con.execute("UPDATE messages SET status='rejeitada' WHERE id=?", (mid,))
    con.commit()
    return redirect(url_for("revisar"))


# --------------------------------------------------------------------- leads
@app.route("/leads")
@need_auth
def leads():
    con = core.db()
    filtro = request.args.get("status", "")
    q = "SELECT * FROM leads" + (" WHERE status=?" if filtro else "") + " ORDER BY id DESC LIMIT 100"
    rows = con.execute(q, (filtro,) if filtro else ()).fetchall()
    linhas = "".join(f'<tr><td><a href="/leads/{l["id"]}">{esc(l["name"])}</a></td>'
                     f'<td>{esc(l["status"])}</td></tr>' for l in rows)
    body = f"""<h2>Leads</h2><table><tr><th>Nome</th><th>Status</th></tr>{linhas}</table>"""
    return page(body)

@app.route("/leads/<int:lid>", methods=["GET", "POST"])
@need_auth
def lead_detail(lid):
    con = core.db()
    if request.method == "POST":
        texto = core.proposal(con, lid, request.form.get("reply", ""))
        l = con.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
        return page(f"<h2>Proposta — {esc(l['name'])}</h2><pre>{esc(texto)}</pre>"
                    f'<a class="btn" href="/leads/{lid}">Voltar</a>')
    l = con.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    if not l:
        return page("<p>Lead não encontrado.</p>")
    msgs = con.execute("SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lid,)).fetchall()
    hist = "".join(f'<div class="card">{esc(m["channel"])} · {esc(m["status"])}<pre>{esc(m["body"])}</pre></div>'
                   for m in msgs) or '<p class="muted">Sem mensagens ainda.</p>'
    contatos = ", ".join(f"{k}: {v}" for k, v in (__import__("json").loads(l["contacts"] or "{}")).items() if v)
    body = f"""<h2>{esc(l['name'])}</h2>
    <p>{esc(l['address'] or '')}<br><span class="muted">Status: {esc(l['status'])} ·
    Tel: {esc(l['phone'] or '-')}</span><br><span class="muted">{esc(contatos)}</span></p>
    <p>{esc(l['notes'] or '')}</p>
    <h3>Gerar proposta (se o lead já respondeu)</h3>
    <form method="post"><textarea name="reply" placeholder="cole aqui o que o lead respondeu"></textarea>
    <button>Gerar proposta</button></form>
    <h3>Histórico de mensagens</h3>{hist}"""
    return page(body)


# ------------------------------------------------------------------ bloqueio
@app.route("/bloqueio", methods=["GET", "POST"])
@need_auth
def bloqueio():
    con = core.db()
    if request.method == "POST":
        v = request.form.get("value", "").strip()
        if v:
            con.execute("INSERT OR IGNORE INTO blocklist VALUES(?)", (core.norm(v),))
            con.commit()
        return redirect(url_for("bloqueio"))
    rows = con.execute("SELECT value FROM blocklist ORDER BY value").fetchall()
    lista = "".join(f"<li>{esc(r['value'])}</li>" for r in rows) or '<p class="muted">Vazia.</p>'
    body = f"""<h2>Não contatar</h2>
    <form method="post" class="card"><input type="text" name="value"
      placeholder="telefone, e-mail ou @usuário"><button>Adicionar</button></form>
    <ul>{lista}</ul>"""
    return page(body)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
