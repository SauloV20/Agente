"""Núcleo do agente de prospecção do Saulo.

Busca negócios sem site (OpenStreetMap primeiro, Google Places para complementar),
escreve as mensagens de apresentação com um LLM (Gemini grátis ou Claude) e controla
o envio. Dependências: apenas `requests` (e `flask` no server.py).
"""
import json, os, re, smtplib, sqlite3, time, urllib.parse
from email.message import EmailMessage

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB = os.environ.get("PROSPECTOR_DB", "prospector.db")
UA = {"User-Agent": "prospector-saulo/1.0 (uso pessoal)"}
CHANNELS = ("whatsapp", "instagram", "facebook", "email", "linkedin")

# Links que NÃO contam como "site próprio" (só rede social / link na bio)
SOCIAL_HOSTS = ("instagram.com", "facebook.com", "fb.com", "linktr.ee", "wa.me",
                "whatsapp.com", "beacons.ai", "bio.link", "ifood.com.br",
                "tiktok.com", "linkedin.com", "goo.gl", "google.com")

# Categorias em português -> (chave OSM, valores). Também aceita "chave=valor".
CATS = {
    "barbearia": ("shop", "hairdresser|barber"), "salão": ("shop", "hairdresser|beauty"),
    "estética": ("shop", "beauty"), "restaurante": ("amenity", "restaurant"),
    "lanchonete": ("amenity", "fast_food|cafe"), "bar": ("amenity", "bar|pub"),
    "padaria": ("shop", "bakery"), "farmácia": ("amenity", "pharmacy"),
    "oficina": ("shop", "car_repair"), "pet shop": ("shop", "pet"),
    "academia": ("leisure", "fitness_centre"), "loja de roupas": ("shop", "clothes"),
    "loja de calçados": ("shop", "shoes"), "mercado": ("shop", "supermarket|convenience"),
    "dentista": ("amenity", "dentist"), "clínica": ("amenity", "clinic|doctors"),
    "açougue": ("shop", "butcher"), "floricultura": ("shop", "florist"),
    "papelaria": ("shop", "stationery"), "ótica": ("shop", "optician"),
    "lavanderia": ("shop", "laundry|dry_cleaning"),
    "material de construção": ("shop", "doityourself|hardware"),
}

# ===== EDITE AQUI: quem você é e o que oferece =====
PERFIL = """
Nome: Saulo. Trabalha com suporte de TI e desenvolve/administra ferramentas de TI.
Serviços que oferece a pequenos negócios:
- Site profissional / catálogo com carrinho e pedido direto pelo WhatsApp
- Painel administrativo para o dono atualizar produtos e preços sozinho
- Chat de atendimento com IA (tira dúvidas, mostra produtos, encaminha o pedido)
- Sistemas simples de controle: chamados, ordens de serviço, checklists de manutenção
- Automação de tarefas repetitivas do dia a dia
Trabalhos reais que PODE citar (cite no máximo um por mensagem, sem inventar detalhes):
- Site da loja Pipo's Tênis: catálogo, carrinho, pedido via WhatsApp, painel admin
  e agente de IA de atendimento
- Sistema de chamados de TI (Central de Chamados) desenvolvido para uso interno
NÃO invente: clientes, resultados, números, prazos, preços ou depoimentos.
"""

ENRICH_SYS = """Você pesquisa canais de contato PÚBLICOS e OFICIAIS de pequenos negócios brasileiros.
Use a busca na web. Não adivinhe: se não encontrar com segurança, use null.
Responda SOMENTE com JSON:
{"website": url|null, "instagram": url|null, "facebook": url|null, "linkedin": url|null,
 "email": string|null, "whatsapp": string|null, "has_system_signals": bool, "notes": string}
has_system_signals = true se houver sinais claros de que o negócio já usa sistema/ERP/
automação/agendamento online/chatbot. notes = 1 frase curta com algo específico e
verdadeiro sobre o negócio (útil para personalizar a abordagem)."""

DRAFT_SYS = f"""Você redige mensagens de primeiro contato em nome do Saulo, em português do Brasil.

SOBRE O SAULO:
{PERFIL}

REGRAS DA MENSAGEM DE APRESENTAÇÃO (a mais importante):
- Escreva na 1ª pessoa, como o Saulo, tom humano, educado e direto. Nada de "Prezado(a)".
- Objetivo: abrir uma conversa, não vender. Nada de pitch longo.
- WhatsApp/Instagram/Facebook/LinkedIn: 3 a 5 linhas. E-mail: até 120 palavras + assunto curto.
- Comece se apresentando e mostrando que olhou o negócio: use só fatos dos dados recebidos
  (nome, bairro, avaliações, notas). Nunca invente nada sobre o negócio.
- Faça UMA observação concreta e verdadeira (ex.: ainda não ter site próprio) e conecte
  com UM serviço relevante para aquele tipo de negócio.
- Máximo um trabalho do portfólio. Sem preço, sem promessa de resultado, sem urgência falsa.
- Termine com uma pergunta simples e uma saída gentil (ex.: "se não fizer sentido, me avisa
  que não incomodo mais"). No máximo 1 emoji. Sem links.
Responda SOMENTE com JSON, com uma chave por canal pedido. Para "email" use
{{"subject": "...", "body": "..."}}; para os demais, uma string."""

PROPOSAL_SYS = f"""Você escreve, em nome do Saulo, uma PROPOSTA curta em português do Brasil
para um pequeno negócio que respondeu ao primeiro contato.

SOBRE O SAULO:
{PERFIL}

Formato (para WhatsApp, sem markdown pesado): 1) uma linha mostrando que entendeu a
necessidade, 2) o que ele faria, em 3 tópicos curtos, 3) como funciona (etapas), 4) valor
como "[VALOR]" e prazo como "[PRAZO]" para o Saulo preencher, 5) próximo passo simples.
Se o lead disse algo na resposta, responda a isso primeiro. Não invente nada."""


# ------------------------------------------------------------------ utilidades
def env(k):
    v = os.environ.get(k)
    if not v:
        raise RuntimeError(f"Defina a variável {k} no .env")
    return v

def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, place_id TEXT UNIQUE, name TEXT,
        address TEXT, phone TEXT, phone_d TEXT, maps_url TEXT, social_link TEXT, rating REAL,
        reviews INTEGER, contacts TEXT, notes TEXT, status TEXT DEFAULT 'novo');
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, lead_id INTEGER, channel TEXT,
        target TEXT, subject TEXT, body TEXT, status TEXT DEFAULT 'rascunho', sent_at TEXT);
    CREATE TABLE IF NOT EXISTS blocklist(value TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS queries(id INTEGER PRIMARY KEY, category TEXT, city TEXT,
        active INTEGER DEFAULT 1, last_run TEXT);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v INTEGER DEFAULT 0);
    """)
    return con

def digits(s):
    return re.sub(r"\D", "", s or "")

def wa_number(phone):
    """Devolve 55DDDNXXXXXXXX se for celular (provável WhatsApp), senão None."""
    d = digits(phone)
    if d.startswith("55") and len(d) in (12, 13):
        pass
    elif len(d) in (10, 11):
        d = "55" + d
    else:
        return None
    nat = d[2:]
    return d if len(nat) == 11 and nat[2] == "9" else None

def first_wa(*vals):
    for v in vals:
        for p in re.split(r"[;,/]", v or ""):
            n = wa_number(p)
            if n:
                return n
    return None

def is_real_site(url):
    if not url:
        return False
    host = urllib.parse.urlparse(url if "//" in url else "//" + url).netloc.lower().removeprefix("www.")
    return not any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)

def norm(value):
    return digits(value) if re.fullmatch(r"[\d\s()+-]+", value or "") else (value or "").strip().lower()

def blocked(con, *values):
    return any(v and con.execute("SELECT 1 FROM blocklist WHERE value=?", (norm(v),)).fetchone()
               for v in values)

def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}

def kv_get(con, k):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else 0

def kv_add(con, k, n=1):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=v+?", (k, n, n))
    con.commit()

def google_key():
    return "google_" + time.strftime("%Y-%m")


# ------------------------------------------------------------------------ LLM
def gemini(system, user, json_out):
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}]}
    if json_out:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    r = requests.post(url, headers={"x-goog-api-key": env("GEMINI_API_KEY")}, json=body, timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def claude(system, user, search, max_tokens):
    body = {"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"), "max_tokens": max_tokens,
            "system": system, "messages": [{"role": "user", "content": user}]}
    if search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=120,
                      headers={"x-api-key": env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text")

def llm(system, user, json_out=True, search=False, max_tokens=1500):
    """Gemini (grátis) se houver GEMINI_API_KEY; senão Claude. Busca web só com Claude."""
    for attempt in range(4):
        try:
            if search or not os.environ.get("GEMINI_API_KEY"):
                return claude(system, user, search, max_tokens)
            return gemini(system, user, json_out)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (429, 503):
                time.sleep(20 * (attempt + 1))
                continue
            raise
    raise RuntimeError("LLM indisponível (limite de uso atingido)")


# ------------------------------------------------------------------ leads/busca
def add_lead(con, pid, name, address, phone, maps_url, social, rating, reviews, contacts):
    d = digits(phone)
    if d and con.execute("SELECT 1 FROM leads WHERE phone_d=?", (d,)).fetchone():
        return 0  # mesmo negócio já veio de outra fonte
    if contacts is None:
        status = "novo"  # aguardando enriquecimento web
    else:
        status = "enriquecido" if any(contacts.values()) else "sem_contato"
    cur = con.execute(
        "INSERT OR IGNORE INTO leads(place_id,name,address,phone,phone_d,maps_url,social_link,"
        "rating,reviews,contacts,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, name, address, phone, d, maps_url, social, rating, reviews,
         json.dumps(contacts) if contacts is not None else None, status))
    return cur.rowcount

def bbox(city):
    r = requests.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=30,
                     params={"q": city, "format": "json", "limit": 1, "countrycodes": "br"})
    r.raise_for_status()
    d = r.json()
    if not d:
        raise ValueError(f"Local não encontrado: {city}")
    s, n, w, e = map(float, d[0]["boundingbox"])
    return s, w, n, e

def osm_contacts(t):
    phone = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or t.get("mobile")
    def soc(v, base):
        if not v:
            return None
        v = v.strip()
        return v if v.startswith("http") else base + v.lstrip("@/")
    return phone, {
        "whatsapp": first_wa(t.get("contact:whatsapp"), phone, t.get("mobile")),
        "email": t.get("email") or t.get("contact:email"),
        "instagram": soc(t.get("contact:instagram"), "https://instagram.com/"),
        "facebook": soc(t.get("contact:facebook"), "https://facebook.com/"),
        "linkedin": None}

def search_osm(con, category, city, cap=300):
    if "=" in category:
        k, v = category.split("=", 1)
    else:
        k, v = CATS.get(category.strip().lower(), (None, None))
        if not k:
            raise ValueError(f"Categoria desconhecida: {category} (use a lista ou chave=valor)")
    s, w, n, e = bbox(city)
    q = f'[out:json][timeout:90];nwr["{k}"~"^({v})$"]["name"]({s},{w},{n},{e});out center tags {cap};'
    last = None
    for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            r = requests.post(url, data={"data": q}, headers=UA, timeout=120)
            r.raise_for_status()
            els = r.json()["elements"]
            break
        except Exception as ex:
            last = ex
    else:
        raise RuntimeError(f"Overpass indisponível: {last}")
    novos = com_site = 0
    for el in els:
        t = el.get("tags", {})
        site = t.get("website") or t.get("contact:website") or t.get("url")
        if is_real_site(site):
            com_site += 1
            continue
        phone, c = osm_contacts(t)
        if not phone and not any(c.values()):
            continue
        addr = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber"), t.get("addr:suburb")) if x)
        novos += add_lead(con, f"osm:{el['type']}/{el['id']}", t["name"], addr, phone,
                          f"https://www.openstreetmap.org/{el['type']}/{el['id']}", site, None, None, c)
    con.commit()
    return novos, com_site

def search_google(con, category, city, max_results=40):
    limit = int(os.environ.get("GOOGLE_MONTHLY_LIMIT", "900"))
    fields = ("places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
              "places.websiteUri,places.googleMapsUri,places.rating,places.userRatingCount,"
              "places.businessStatus,nextPageToken")
    headers = {"X-Goog-Api-Key": env("GOOGLE_API_KEY"), "X-Goog-FieldMask": fields}
    body = {"textQuery": f"{category} em {city}", "languageCode": "pt-BR", "regionCode": "BR", "pageSize": 20}
    out = []
    while len(out) < max_results and kv_get(con, google_key()) < limit:
        r = requests.post("https://places.googleapis.com/v1/places:searchText",
                          headers=headers, json=body, timeout=30)
        kv_add(con, google_key())
        r.raise_for_status()
        data = r.json()
        out += data.get("places", [])
        if not data.get("nextPageToken"):
            break
        body["pageToken"] = data["nextPageToken"]
        time.sleep(1)
    web = os.environ.get("WEB_ENRICH") == "1" and os.environ.get("ANTHROPIC_API_KEY")
    novos = com_site = 0
    for p in out:
        if p.get("businessStatus") != "OPERATIONAL":
            continue
        site = p.get("websiteUri")
        if is_real_site(site):
            com_site += 1
            continue
        phone = p.get("nationalPhoneNumber")
        c = None if web else {"whatsapp": wa_number(phone), "email": None, "linkedin": None,
                              "instagram": site if "instagram.com" in (site or "") else None,
                              "facebook": site if "facebook.com" in (site or "") else None}
        novos += add_lead(con, p["id"], p["displayName"]["text"], p.get("formattedAddress"), phone,
                          p.get("googleMapsUri"), site, p.get("rating"), p.get("userRatingCount"), c)
    con.commit()
    return novos, com_site


# ------------------------------------------------------- enriquecer / rascunhar
def enrich(con, limit, log):
    """Busca web de canais (precisa de ANTHROPIC_API_KEY). Só roda com WEB_ENRICH=1."""
    for l in con.execute("SELECT * FROM leads WHERE status='novo' LIMIT ?", (limit,)).fetchall():
        prompt = (f"Negócio: {l['name']}\nEndereço: {l['address']}\nTelefone: {l['phone']}\n"
                  f"Link no Google: {l['social_link'] or 'nenhum'}")
        try:
            info = parse_json(llm(ENRICH_SYS, prompt, search=True))
        except Exception as e:
            log(f"enriquecer falhou ({l['name']}): {e}")
            return
        if is_real_site(info.get("website")) or info.get("has_system_signals"):
            con.execute("UPDATE leads SET status='descartado', notes=? WHERE id=?",
                        (f"já tem site/sistema: {info.get('website') or info.get('notes')}", l["id"]))
        else:
            c = {k: info.get(k) for k in ("instagram", "facebook", "linkedin", "email")}
            c["whatsapp"] = first_wa(info.get("whatsapp"), l["phone"])
            link = l["social_link"] or ""
            if "instagram.com" in link and not c["instagram"]:
                c["instagram"] = link
            if "facebook.com" in link and not c["facebook"]:
                c["facebook"] = link
            con.execute("UPDATE leads SET contacts=?, notes=?, status='enriquecido' WHERE id=?",
                        (json.dumps(c), info.get("notes"), l["id"]))
        con.commit()

def draft(con, limit, log):
    for l in con.execute("SELECT * FROM leads WHERE status='enriquecido' LIMIT ?", (limit,)).fetchall():
        c = json.loads(l["contacts"] or "{}")
        canais = [k for k in CHANNELS if c.get(k) and not blocked(con, c[k], l["phone"])]
        if not canais:
            con.execute("UPDATE leads SET status='sem_contato' WHERE id=?", (l["id"],))
            con.commit()
            continue
        avaliacao = f"Nota Google: {l['rating']} ({l['reviews']} avaliações)\n" if l["rating"] else ""
        prompt = (f"Negócio: {l['name']}\nEndereço: {l['address']}\n{avaliacao}"
                  f"Observação: {l['notes'] or '-'}\nAinda sem site próprio: sim\n"
                  f"Canais pedidos: {', '.join(canais)}")
        try:
            out = parse_json(llm(DRAFT_SYS, prompt, max_tokens=2000))
        except Exception as e:
            log(f"rascunho falhou ({l['name']}): {e}")
            return
        for ch in canais:
            m = out.get(ch)
            if ch == "email" and isinstance(m, dict) and m.get("body"):
                subject, body = m.get("subject") or "Apresentação", m["body"]
            elif ch != "email" and isinstance(m, str) and m.strip():
                subject, body = None, m
            else:
                continue
            con.execute("INSERT INTO messages(lead_id,channel,target,subject,body) VALUES(?,?,?,?,?)",
                        (l["id"], ch, c[ch], subject, body))
        con.execute("UPDATE leads SET status='rascunhado' WHERE id=?", (l["id"],))
        con.commit()
        log(f"rascunho pronto: {l['name']} ({', '.join(canais)})")
        time.sleep(float(os.environ.get("LLM_DELAY", "8")))  # respeita limite do plano grátis


# --------------------------------------------------------------------- envio
FOOTER = "\n\n--\nSe preferir não receber mais contatos meus, é só responder \"parar\"."

def send_email(to, subject, body):
    msg = EmailMessage()
    msg["From"] = f"{os.environ.get('FROM_NAME', 'Saulo')} <{env('SMTP_USER')}>"
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body + FOOTER)
    with smtplib.SMTP(env("SMTP_HOST"), int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(env("SMTP_USER"), env("SMTP_PASS"))
        s.send_message(msg)

def mark_sent(con, mid):
    m = con.execute("SELECT lead_id FROM messages WHERE id=?", (mid,)).fetchone()
    con.execute("UPDATE messages SET status='enviada', sent_at=datetime('now') WHERE id=?", (mid,))
    con.execute("UPDATE messages SET status='ignorada' WHERE lead_id=? AND id<>? "
                "AND status IN ('rascunho','aprovada')", (m["lead_id"], mid))
    con.execute("UPDATE leads SET status='contatado' WHERE id=?", (m["lead_id"],))
    con.commit()

def send_approved_emails(con, log):
    cap = int(os.environ.get("EMAIL_DAILY_LIMIT", "15"))
    hoje = con.execute("SELECT COUNT(*) c FROM messages WHERE channel='email' AND status='enviada' "
                       "AND date(sent_at)=date('now')").fetchone()["c"]
    rows = con.execute("SELECT m.*, l.phone FROM messages m JOIN leads l ON l.id=m.lead_id "
                       "WHERE m.channel='email' AND m.status='aprovada' ORDER BY m.id").fetchall()
    for m in rows:
        if hoje >= cap:
            break
        ja = con.execute("SELECT 1 FROM messages WHERE lead_id=? AND status='enviada'", (m["lead_id"],)).fetchone()
        if ja or blocked(con, m["target"], m["phone"]):
            con.execute("UPDATE messages SET status='ignorada' WHERE id=?", (m["id"],))
            con.commit()
            continue
        try:
            send_email(m["target"], m["subject"], m["body"])
        except Exception as e:
            log(f"e-mail falhou: {e}")
            break
        mark_sent(con, m["id"])
        hoje += 1
        log(f"e-mail enviado para {m['target']}")
        time.sleep(45)

def proposal(con, lead_id, reply):
    l = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    prompt = (f"Negócio: {l['name']}\nEndereço: {l['address']}\nObservação: {l['notes'] or '-'}\n"
              f"Resposta do lead: {reply or '(sem detalhes)'}")
    text = llm(PROPOSAL_SYS, prompt, json_out=False)
    con.execute("UPDATE leads SET status='respondeu' WHERE id=?", (lead_id,))
    con.commit()
    return text


# ---------------------------------------------------------------- ciclo 24h
def run_cycle(log):
    con = db()
    q = con.execute("SELECT * FROM queries WHERE active=1 ORDER BY COALESCE(last_run,'') LIMIT 1").fetchone()
    if q:
        try:
            n, s = search_osm(con, q["category"], q["city"])
            log(f"OSM {q['category']} / {q['city']}: {n} novos, {s} já têm site")
        except Exception as e:
            log(f"OSM falhou: {e}")
        if os.environ.get("GOOGLE_API_KEY") and "=" not in q["category"]:
            try:
                n, s = search_google(con, q["category"], q["city"])
                log(f"Google {q['category']} / {q['city']}: {n} novos, {s} já têm site")
            except Exception as e:
                log(f"Google falhou: {e}")
        con.execute("UPDATE queries SET last_run=datetime('now') WHERE id=?", (q["id"],))
        con.commit()
    else:
        log("nenhuma busca ativa — adicione uma na aba Buscas")
    if os.environ.get("WEB_ENRICH") == "1" and os.environ.get("ANTHROPIC_API_KEY"):
        enrich(con, 5, log)
    pend = con.execute("SELECT COUNT(*) c FROM messages WHERE status='rascunho'").fetchone()["c"]
    if pend < int(os.environ.get("QUEUE_MAX", "30")):
        draft(con, int(os.environ.get("DRAFT_PER_CYCLE", "5")), log)
    send_approved_emails(con, log)
