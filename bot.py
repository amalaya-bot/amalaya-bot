#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time
from urllib.parse import urljoin, urlparse

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

HEADERS = {"User-Agent": "AmalayaBot/1.0 (+https://amalaya.com.co)"}

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None

# Categorías definidas en Amalaya (exactas)
ALLOWED_CATEGORIES = ["Historia", "lanzamientos", "videos", "artistas", "festivales", "podcasts"]

# Tags base (exactos)
BASE_TAGS = ["Diomedes Diaz", "Silvestre Dangond", "vallenato", "binomio de oro", "secundarios"]


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

def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc

def esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def normalize_category_name(name: str) -> str:
    # En WP a veces “Historia” tiene mayúscula y las otras no, respetamos lista.
    # Si te gusta todo minúscula, cámbialo en WP y aquí.
    for c in ALLOWED_CATEGORIES:
        if c.lower() == name.lower():
            return c
    return "artistas"


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

def wp_headers_upload(filename: str) -> dict:
    return {
        **HEADERS,
        "Authorization": f"Bearer {get_jwt_token()}",
        "Content-Disposition": f'attachment; filename="{filename}"'
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

    # Si por alguna razón no existe, la creamos (no debería pasar si ya existen)
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
    # Se queda en el editor para tu plugin, pero no se verá al público con CSS
    return f'<!-- wp:paragraph {{"className":"amalaya-meta"}} -->\n<p class="amalaya-meta">{esc_html(clean_text(text))}</p>\n<!-- /wp:paragraph -->\n'

def render_image_block(media_id: int, media_url: str) -> str:
    return (
        f'<!-- wp:image {{"id":{media_id},"sizeSlug":"large"}} -->\n'
        f'<figure class="wp-block-image size-large"><img src="{media_url}" class="wp-image-{media_id}"/></figure>\n'
        f'<!-- /wp:image -->\n'
    )


# =========================
# Sources & link collection
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

    # Bloqueos típicos (secciones, páginas legales, etc.)
    blocked = [
        "politica-de-privacidad", "privacidad", "privacy", "terms", "terminos", "servicios",
        "contacto", "about", "acerca", "cookies", "avisolegal",
        "/tag/", "/category/", "/categoria/", "/author/", "/page/", "/search", "/wp-content/"
    ]
    low = url.lower()
    if any(b in low for b in blocked):
        return False

    # Heurísticas de artículo: (a) contiene año / (b) slug largo con guiones
    if re.search(r"/20\d{2}/", low):
        return True
    if p.count("-") >= 2 and len(p) >= 18:
        return True

    return False

def extract_article_links_from_home(site_url: str, limit: int = 40) -> list[str]:
    try:
        r = requests.get(site_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        links = []

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            full = urljoin(site_url, href)

            if not same_domain(full, site_url):
                continue

            if looks_like_article(full):
                links.append(full)

        out, seen = [], set()
        for u in links:
            if u not in seen:
                out.append(u)
                seen.add(u)
            if len(out) >= limit:
                break

        return out
    except Exception:
        return []

def collect_candidate_urls(sites: list[str], seen: set[str], per_site: int = 35) -> list[str]:
    out = []
    for site in sites:
        site = site.strip()
        if not site.startswith("http"):
            site = "https://" + site

        feed_url = discover_feed(site)
        urls = []

        if feed_url:
            f = feedparser.parse(feed_url)
            for e in getattr(f, "entries", [])[:per_site]:
                link = getattr(e, "link", None)
                if link and looks_like_article(link):
                    urls.append(link)
        else:
            urls = extract_article_links_from_home(site, limit=per_site)

        for u in urls:
            if u not in seen and u not in out:
                out.append(u)

        time.sleep(1)

    return out


# =========================
# Article parsing
# =========================
def get_article_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text

def get_article_title_snippet(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.text:
        title = soup.title.text.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)

    snippet = ""
    for p in soup.find_all("p"):
        txt = clean_text(p.get_text(" ", strip=True))
        if len(txt) >= 90:
            snippet = txt
            break

    return (clean_text(title)[:140], clean_text(snippet)[:220])

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

    # Debe tocar vallenato o su universo (amplio)
    must_any = [
        "vallenato", "acordeón", "acordeon", "guacharaca", "caja", "juglar", "juglares",
        "valledupar", "festival vallenato", "parque de la leyenda",
        "rey vallenato", "reina vallenata", "piqueria", "piquería",
        "diomedes", "silvestre", "binomio de oro", "poncho zuleta", "emiliano zuleta",
        "martín elías", "martin elias", "rafa pérez", "rafa perez",
        "jorge celedón", "jorge celedon", "los inquietos", "iván villazón", "ivan villazon"
    ]

    # Señales fuertes de "no vallenato"
    blocked = [
        "registradur", "elecciones", "concejo", "congreso", "fiscalía", "fiscalia", "corte",
        "judicial", "homicidio", "captura", "policía", "policia",
        "deportes", "fútbol", "futbol", "liga", "selección", "seleccion",
        "economía", "inflación", "inflacion", "dólar", "dolar"
    ]

    # Si tiene bloqueo y NO tiene ninguna palabra vallenata, se descarta
    if any(b in t for b in blocked) and not any(k in t for k in must_any):
        return False

    return any(k in t for k in must_any)


# =========================
# OpenAI generation (Responses API)
# =========================
def openai_generate(article_text: str, source_url: str, title_hint: str, snippet_hint: str) -> dict:
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
Snippet sugerido: {snippet_hint}

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
    seo = clean_text(obj.get("seo", ""))[:160] or (clean_text(snippet_hint)[:160] or "Actualidad del vallenato.")
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

    # Si no detectó ninguna “estrella”, cae a secundarios
    if len(tags) == 1:
        tags.append("secundarios")

    # Asegurar que sean tags permitidos (base)
    out = []
    for t in tags:
        if t in BASE_TAGS and t not in out:
            out.append(t)

    # si por alguna razón quedó vacío, deja vallenato
    if not out:
        out = ["vallenato", "secundarios"]

    return out[:6]


# =========================
# Image (upload + featured)
# =========================
def get_og_image(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"].strip()
    return None

def download_image(img_url: str) -> tuple[bytes, str] | None:
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").lower()
        ext = "jpg"
        if "png" in ctype:
            ext = "png"
        elif "webp" in ctype:
            ext = "webp"
        elif "jpeg" in ctype or "jpg" in ctype:
            ext = "jpg"
        return (r.content, ext)
    except Exception:
        return None

def upload_media_to_wp(image_bytes: bytes, ext: str, filename_hint="imagen") -> dict:
    endpoint = f"{WP_URL}/wp-json/wp/v2/media"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", filename_hint.lower()).strip("-")[:50] or "imagen"
    filename = f"{safe}.{ext}"

    r = requests.post(endpoint, headers=wp_headers_upload(filename), data=image_bytes, timeout=35)
    r.raise_for_status()
    j = r.json()
    return {"id": int(j["id"]), "source_url": j.get("source_url") or j.get("guid", {}).get("rendered")}


# =========================
# Create WP Post (featured + taxonomies)
# =========================
def create_post_in_wp(title: str, content_html: str, featured_media_id: int | None, category_ids: list[int], tag_ids: list[int]) -> dict:
    endpoint = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title": title,
        "content": content_html,
        "status": WP_POST_STATUS,
        "categories": category_ids,
        "tags": tag_ids
    }
    if featured_media_id:
        payload["featured_media"] = featured_media_id

    r = requests.post(endpoint, headers=wp_headers_json(), json=payload, timeout=40)
    r.raise_for_status()
    return r.json()


# =========================
# MAIN
# =========================
def main():
    sources = load_json(SOURCES_PATH, {"sites": [], "max_posts_per_run": POSTS_PER_RUN})
    state = load_json(STATE_PATH, {"seen_urls": []})

    seen = set(state.get("seen_urls", []))
    sites = sources.get("sites", [])
    maxn = int(sources.get("max_posts_per_run", POSTS_PER_RUN))

    if not sites:
        print("ERROR: sources.json no tiene 'sites'.")
        return

    candidates = collect_candidate_urls(sites, seen, per_site=35)
    if not candidates:
        print("No encontré URLs nuevas.")
        return

    picked = candidates[:maxn]

    for url in picked:
        try:
            html = get_article_html(url)
            title_hint, snippet_hint = get_article_title_snippet(html)
            article_text = extract_article_text(html)

            # 1) filtro vallenato (antes de gastar IA)
            if not is_vallenato_related(title_hint, article_text):
                print(f"SKIP (no vallenato): {url}")
                seen.add(url)
                continue

            # 2) generar nota con IA
            article = openai_generate(article_text, url, title_hint, snippet_hint)
            title = article["title"]
            seo = article["seo"]
            paragraphs = article["paragraphs"]

            # 3) categoría/tags
            full_text = " ".join([title, seo] + paragraphs)
            category_name = normalize_category_name(pick_category(full_text))
            tag_names = tags_from_text(full_text)

            # Convertir a IDs reales WP
            cat_id = ensure_category(category_name)
            tag_ids = [ensure_tag(t) for t in tag_names]

            # 4) imagen destacada
            featured_id = None
            image_block = ""
            og_img = get_og_image(html)
            if og_img:
                try:
                    dl = download_image(og_img)
                    if dl:
                        img_bytes, ext = dl
                        media = upload_media_to_wp(img_bytes, ext, filename_hint=title)
                        featured_id = media["id"]
                        # opcional: además del featured, lo ponemos dentro del contenido
                        image_block = render_image_block(media["id"], media["source_url"])
                except Exception:
                    featured_id = None
                    image_block = ""

            # 5) armar contenido plugin (p1)
            parts = []
            parts.append(render_p1(title))
            parts.append(render_p1(seo))

            # primer párrafo
            parts.append(render_p1(paragraphs[0]))

            # imagen (si existe)
            if image_block:
                parts.append(image_block)

            # resto de párrafos
            for p in paragraphs[1:]:
                parts.append(render_p1(p))

            # 6) Mantener tu plugin content meta PERO OCULTO AL PÚBLICO
            # (antepenúltima: categoría; penúltima: etiquetas; última: #autopublicar)
            parts.append(render_hidden(category_name))
            parts.append(render_hidden(", ".join(tag_names)))
            parts.append(render_hidden("#autopublicar"))

            content_html = "\n".join(parts)

            post = create_post_in_wp(
                title=title,
                content_html=content_html,
                featured_media_id=featured_id,
                category_ids=[cat_id],
                tag_ids=tag_ids
            )

            print(f"OK: {post.get('id')} - {post.get('link')}")
            seen.add(url)
            time.sleep(2)

        except Exception as e:
            print(f"ERROR con {url}: {e}")
            time.sleep(1)

    # guardar “seen” en state.json (nota: para que persista entre runs, luego lo comiteamos en Actions)
    state["seen_urls"] = list(seen)[-2000:]
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
