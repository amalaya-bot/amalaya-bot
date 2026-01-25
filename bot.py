#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
import feedparser
from bs4 import BeautifulSoup

# =========================
# Config (env)
# =========================
WP_URL = os.environ.get("WP_URL", "https://amalaya.com.co").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_PASSWORD = os.environ.get("WP_PASSWORD", "")
WP_POST_STATUS = os.environ.get("WP_POST_STATUS", "draft")  # draft | publish

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "10"))
MIN_PARAGRAPHS = int(os.environ.get("MIN_PARAGRAPHS", "4"))

HEADERS = {"User-Agent": "AmalayaBot/1.2 (+https://amalaya.com.co)"}

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None

# Categorías definidas en Amalaya (exactas)
ALLOWED_CATEGORIES = ["Historia", "lanzamientos", "videos", "artistas", "festivales", "podcasts"]

# Tags base (exactos)
BASE_TAGS = ["Diomedes Diaz", "Silvestre Dangond", "vallenato", "binomio de oro", "secundarios"]

# Ventana 24h estricta
WINDOW_HOURS = 24


# =========================
# Helpers
# =========================
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def clean_text(s: str) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc

def normalize_category_name(name: str) -> str:
    for c in ALLOWED_CATEGORIES:
        if c.lower() == (name or "").lower():
            return c
    return "artistas"

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def within_last_hours(dt: datetime, hours: int) -> bool:
    if not dt:
        return False
    return dt >= (now_utc() - timedelta(hours=hours))

def parse_dt_safe(s: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def parse_date_from_url(url: str) -> datetime | None:
    """
    Heurística: si la URL contiene /YYYY/MM/DD/ o /YYYY-MM-DD/ intenta parsear.
    Si no, devuelve None.
    """
    u = url.lower()
    m = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", u)
    if m:
        y, mo, d = map(int, m.groups())
        return datetime(y, mo, d, tzinfo=timezone.utc)
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", u)
    if m:
        y, mo, d = map(int, m.groups())
        return datetime(y, mo, d, tzinfo=timezone.utc)
    return None


# =========================
# JWT Auth
# =========================
def get_jwt_token() -> str:
    global JWT_TOKEN
    if JWT_TOKEN:
        return JWT_TOKEN

    if not WP_USER or not WP_PASSWORD:
        raise RuntimeError("Faltan WP_USER o WP_PASSWORD en Secrets")

    url = f"{WP_URL}/wp-json/jwt-auth/v1/token"
    payload = {"username": WP_USER, "password": WP_PASSWORD}
    r = requests.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"JWT token error {r.status_code}: {r.text[:300]}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"No token en respuesta JWT: {r.text[:300]}")
    JWT_TOKEN = token
    return token

def wp_headers_json() -> dict:
    return {**HEADERS, "Authorization": f"Bearer {get_jwt_token()}", "Content-Type": "application/json"}

def wp_headers_upload(filename: str, content_type: str) -> dict:
    return {
        **HEADERS,
        "Authorization": f"Bearer {get_jwt_token()}",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": content_type
    }


# =========================
# WP Taxonomies (categorías / etiquetas reales)
# =========================
def ensure_category(name: str) -> int:
    name = normalize_category_name(name)
    endpoint = f"{WP_URL}/wp-json/wp/v2/categories"

    r = requests.get(endpoint, headers=wp_headers_json(), params={"search": name}, timeout=25)
    r.raise_for_status()
    items = r.json() or []
    for it in items:
        if (it.get("name") or "").strip().lower() == name.strip().lower():
            return int(it["id"])

    r = requests.post(endpoint, headers=wp_headers_json(), json={"name": name}, timeout=25)
    r.raise_for_status()
    return int(r.json()["id"])

def ensure_tag(name: str) -> int:
    endpoint = f"{WP_URL}/wp-json/wp/v2/tags"

    r = requests.get(endpoint, headers=wp_headers_json(), params={"search": name}, timeout=25)
    r.raise_for_status()
    items = r.json() or []
    for it in items:
        if (it.get("name") or "").strip().lower() == name.strip().lower():
            return int(it["id"])

    r = requests.post(endpoint, headers=wp_headers_json(), json={"name": name}, timeout=25)
    r.raise_for_status()
    return int(r.json()["id"])


# =========================
# Gutenberg blocks (formato plugin)
# =========================
def render_p1(text: str) -> str:
    return f'<!-- wp:paragraph {{"className":"p1"}} -->\n<p class="p1">{esc_html(clean_text(text))}</p>\n<!-- /wp:paragraph -->\n'

def render_hidden(text: str) -> str:
    # Plugin lo lee, público no lo ve si pones el CSS .amalaya-meta {display:none}
    return f'<!-- wp:paragraph {{"className":"amalaya-meta"}} -->\n<p class="amalaya-meta">{esc_html(clean_text(text))}</p>\n<!-- /wp:paragraph -->\n'

def render_image_block(media_id: int, media_url: str) -> str:
    return (
        f'<!-- wp:image {{"id":{media_id},"sizeSlug":"large"}} -->\n'
        f'<figure class="wp-block-image size-large"><img src="{media_url}" class="wp-image-{media_id}"/></figure>\n'
        f'<!-- /wp:image -->\n'
    )


# =========================
# Sources & link collection (24h strict)
# =========================
FEED_CANDIDATES = ["feed/", "feed", "rss", "rss/", "rss.xml", "feed.xml", "atom.xml", "?feed=rss2"]

def discover_feed(site_url: str) -> str | None:
    try:
        r = requests.get(site_url, headers=HEADERS, timeout=20)
        if r.status_code == 200 and "text/html" in (r.headers.get("Content-Type") or ""):
            soup = BeautifulSoup(r.text, "html.parser")
            for link in soup.find_all("link", attrs={"rel": "alternate"}):
                t = (link.get("type") or "").lower()
                href = link.get("href")
                if href and ("rss" in t or "atom" in t or "xml" in t):
                    return urljoin(site_url, href)
    except Exception:
        pass

    base = site_url if site_url.endswith("/") else site_url + "/"
    for c in FEED_CANDIDATES:
        candidate = urljoin(base, c)
        try:
            f = feedparser.parse(candidate)
            if getattr(f, "entries", None) and len(f.entries) > 0:
                return candidate
        except Exception:
            continue
    return None

def looks_like_article(url: str) -> bool:
    p = urlparse(url).path.strip("/").lower()
    if not p:
        return False

    blocked = [
        "politica-de-privacidad", "privacidad", "privacy", "terms", "terminos", "servicios",
        "contacto", "cookies", "avisolegal",
        "/tag/", "/category/", "/categoria/", "/author/", "/page/", "/search", "/wp-content/"
    ]
    low = url.lower()
    if any(b in low for b in blocked):
        return False

    # Heurística artículo
    if re.search(r"/20\d{2}/", low):
        return True
    if p.count("-") >= 2 and len(p) >= 18:
        return True
    return False

def collect_candidate_urls_last24h(sites: list[str], seen: set[str], per_site: int = 60) -> list[tuple[str, datetime]]:
    """
    Devuelve lista de (url, published_dt_utc) SOLO si está dentro de 24h.
    Estricto: si no hay feed o no hay fecha, se descarta.
    """
    out: list[tuple[str, datetime]] = []

    for site in sites:
        site = site.strip()
        if not site.startswith("http"):
            site = "https://" + site

        feed_url = discover_feed(site)
        if not feed_url:
            print(f"INFO: {site} sin feed detectado -> se omite para filtro 24h.")
            time.sleep(1)
            continue

        f = feedparser.parse(feed_url)
        entries = getattr(f, "entries", [])[:per_site]

        for e in entries:
            link = getattr(e, "link", None)
            if not link or not looks_like_article(link):
                continue
            if link in seen:
                continue

            pub_dt = None
            if hasattr(e, "published") and getattr(e, "published", None):
                pub_dt = parse_dt_safe(e.published)
            elif hasattr(e, "updated") and getattr(e, "updated", None):
                pub_dt = parse_dt_safe(e.updated)

            # Si no hay fecha confiable en feed, intentamos URL-date; si tampoco, descartar
            if not pub_dt:
                pub_dt = parse_date_from_url(link)

            if not pub_dt:
                continue

            if within_last_hours(pub_dt, WINDOW_HOURS):
                out.append((link, pub_dt))

        time.sleep(1)

    # Ordenar por más reciente (global)
    out.sort(key=lambda x: x[1], reverse=True)
    return out


# =========================
# Article parsing
# =========================
def get_article_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text

def get_article_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    elif soup.title and soup.title.text:
        title = soup.title.text.strip()
    return clean_text(title)[:140]

def extract_article_text(html: str, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    main = soup.find("article") or soup.find("main") or soup.body
    if not main:
        return ""

    paras = []
    for p in main.find_all("p"):
        txt = clean_text(p.get_text(" ", strip=True))
        if len(txt) >= 40:
            paras.append(txt)

    return ("\n".join(paras))[:max_chars]


# =========================
# Vallenato filter
# =========================
def is_vallenato_related(title: str, text: str) -> bool:
    t = (title + " " + text).lower()

    must_any = [
        "vallenato", "acordeón", "acordeon", "guacharaca", "caja", "juglar", "juglares",
        "valledupar", "festival vallenato", "parque de la leyenda",
        "rey vallenato", "reina vallenata", "piqueria", "piquería",
        "diomedes", "silvestre", "binomio de oro", "poncho zuleta", "emiliano zuleta",
        "martín elías", "martin elias", "rafa pérez", "rafa perez",
        "jorge celedón", "jorge celedon", "iván villazón", "ivan villazon"
    ]

    blocked = [
        "registradur", "elecciones", "concejo", "congreso", "fiscalía", "fiscalia", "corte",
        "judicial", "homicidio", "captura", "policía", "policia",
        "deportes", "fútbol", "futbol", "liga", "selección", "seleccion",
        "economía", "inflación", "inflacion", "dólar", "dolar"
    ]

    if any(b in t for b in blocked) and not any(k in t for k in must_any):
        return False

    return any(k in t for k in must_any)


# =========================
# OpenAI generation (Responses API)
# =========================
def openai_generate(article_text: str, source_url: str, title_hint: str) -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("Falta OPENAI_API_KEY")

    system = (
        "Eres editor de un medio vallenato llamado Amalaya. "
        "Redactas notas originales, informativas y verificables. "
        "NO inventes datos. NO copies frases largas del texto fuente. "
        "Si algo no está confirmado, dilo explícitamente. "
        "Tono neutral, sin emojis."
    )

    user = f"""
Fuente (URL): {source_url}

Título sugerido: {title_hint}

Texto fuente (solo para extraer hechos; NO copiar literalmente):
\"\"\"{article_text[:12000]}\"\"\"

Tarea:
- Escribe una noticia NUEVA para Amalaya basada en hechos verificables del texto fuente.
- Devuelve SOLO JSON válido con: title, seo (máx 160), paragraphs (lista).
- Mínimo {MIN_PARAGRAPHS} párrafos. Usa los párrafos necesarios (no número fijo).
- En el último párrafo incluye: "Información basada en la fuente consultada."
JSON:
{{
  "title": "...",
  "seo": "...",
  "paragraphs": ["...","..."]
}}
""".strip()

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        "max_output_tokens": 900
    }

    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=80
    )
    r.raise_for_status()
    data = r.json()

    out_text = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") in ("output_text", "text"):
                out_text += c.get("text", "")

    out_text = out_text.strip()

    try:
        obj = json.loads(out_text)
    except Exception:
        m = re.search(r"\{.*\}", out_text, flags=re.DOTALL)
        if not m:
            raise RuntimeError("No pude parsear JSON de IA: " + out_text[:300])
        obj = json.loads(m.group(0))

    title = clean_text(obj.get("title", ""))[:140] or (clean_text(title_hint)[:140] or "Noticia vallenata")
    seo = clean_text(obj.get("seo", ""))[:160] or "Actualidad del vallenato."
    paragraphs = [clean_text(p) for p in obj.get("paragraphs", []) if clean_text(p)]

    while len(paragraphs) < MIN_PARAGRAPHS:
        paragraphs.append("Amalaya seguirá verificando información para ampliar cuando haya detalles confirmados.")

    return {"title": title, "seo": seo, "paragraphs": paragraphs}


# =========================
# Category & tags (lógica Amalaya)
# =========================
def pick_category(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["lanzamiento", "estreno", "sencillo", "álbum", "album", "disco", "ep"]):
        return "lanzamientos"
    if any(k in t for k in ["video", "videoclip", "youtube", "clip"]):
        return "videos"
    if any(k in t for k in ["festival", "parque de la leyenda", "rey vallenato", "reina vallenata", "piqueria", "piquería"]):
        return "festivales"
    if any(k in t for k in ["podcast", "episodio", "capítulo", "capitulo"]):
        return "podcasts"
    if any(k in t for k in ["historia", "homenaje", "aniversario", "biografía", "biografia", "juglar", "juglares"]):
        return "Historia"
    return "artistas"

def tags_from_text(text: str) -> list[str]:
    low = text.lower()
    tags = ["vallenato"]

    if "silvestre" in low:
        tags.append("Silvestre Dangond")
    if "diomedes" in low:
        tags.append("Diomedes Diaz")
    if "binomio" in low:
        tags.append("binomio de oro")

    if len(tags) == 1:
        tags.append("secundarios")

    out = []
    for t in tags:
        if t in BASE_TAGS and t not in out:
            out.append(t)

    if not out:
        out = ["vallenato", "secundarios"]

    return out[:6]


# =========================
# Image (best effort: og/twitter/img + referer + content-type)
# =========================
def get_best_image_url(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"].strip()

    main = soup.find("article") or soup.find("main") or soup.body
    if main:
        img =
