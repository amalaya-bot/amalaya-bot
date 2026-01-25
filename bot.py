#!/usr/bin/env python3
import os, re, json, time
from urllib.parse import urljoin, urlparse
import requests
import feedparser
from bs4 import BeautifulSoup

# =========================
# WordPress (Amalaya) JWT
# =========================
WP_URL = os.environ.get("WP_URL", "https://amalaya.com.co").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_PASSWORD = os.environ.get("WP_PASSWORD", "")
WP_POST_STATUS = os.environ.get("WP_POST_STATUS", "draft")  # draft o publish

# =========================
# OpenAI API
# =========================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "10"))
MIN_PARAGRAPHS = int(os.environ.get("MIN_PARAGRAPHS", "4"))

ALLOWED_CATEGORIES = ["historia", "lanzamientos", "videos", "artistas", "festivales", "podcasts"]

HEADERS = {"User-Agent": "AmalayaBot/1.0 (+https://amalaya.com.co)"}
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None  # cache en runtime


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


# -------------------------
# JWT Auth
# -------------------------
def get_jwt_token() -> str:
    global JWT_TOKEN
    if JWT_TOKEN:
        return JWT_TOKEN

    if not (WP_USER and WP_PASSWORD):
        raise RuntimeError("Faltan WP_USER o WP_PASSWORD (Secrets)")

    url = f"{WP_URL}/wp-json/jwt-auth/v1/token"
    payload = {"username": WP_USER, "password": WP_PASSWORD}
    r = requests.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"JWT token error {r.status_code}: {r.text[:300]}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"No token in response: {r.text[:300]}")
    JWT_TOKEN = token
    return token

def wp_headers_json() -> dict:
    token = get_jwt_token()
    return {**HEADERS, "Authorization": f"Bearer {token}"}


# -------------------------
# Feed discovery
# -------------------------
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


def extract_article_links_from_home(site_url: str, limit: int = 30) -> list[str]:
    try:
        r = requests.get(site_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        bad_fragments = [
            "#", "/tag/", "/category/", "/categoria/", "/author/", "/wp-content/",
            "/page/", "/contact", "/privacy", "/terms", "/search",
            "mailto:", "javascript:",
            "politica-de-privacidad", "privacy", "servicios"
        ]

        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            full = urljoin(site_url, href)

            if not same_domain(full, site_url):
                continue

            low = full.lower()
            if any(x in low for x in bad_fragments):
                continue

            path = urlparse(full).path.strip("/")
            if len(path) < 8:
                continue

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


def collect_candidate_urls(sites: list[str], seen: set[str], per_site: int = 25) -> list[str]:
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
                if link:
                    urls.append(link)
        else:
            urls = extract_article_links_from_home(site, limit=per_site)

        for u in urls:
            if u not in seen and u not in out:
                out.append(u)

        time.sleep(1)

    return out


# -------------------------
# Article parsing
# -------------------------
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


# -------------------------
# Image upload
# -------------------------
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
    headers = {
        **wp_headers_json(),
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    r = requests.post(endpoint, headers=headers, data=image_bytes, timeout=30)
    r.raise_for_status()
    j = r.json()
    return {"id": j["id"], "source_url": j.get("source_url") or j.get("guid", {}).get("rendered")}


# -------------------------
# Gutenberg blocks
# -------------------------
def render_p1(text: str) -> str:
    safe = clean_text(text)
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<!-- wp:paragraph {{"className":"p1"}} -->\n<p class="p1">{safe}</p>\n<!-- /wp:paragraph -->\n'

def render_image_block(media_id: int, media_url: str) -> str:
    return (
        f'<!-- wp:image {{"id":{media_id},"sizeSlug":"large"}} -->\n'
        f'<figure class="wp-block-image size-large"><img src="{media_url}" class="wp-image-{media_id}"/></figure>\n'
        f'<!-- /wp:image -->\n'
    )


def create_post_in_wp(title: str, content_html: str) -> dict:
    endpoint = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": content_html, "status": WP_POST_STATUS}
    r = requests.post(endpoint, headers=wp_headers_json(), json=payload, timeout=35)
    r.raise_for_status()
    return r.json()


# -------------------------
# Category & tags
# -------------------------
def pick_category(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["lanzamiento", "estreno", "sencillo", "álbum", "album", "disco"]):
        return "lanzamientos"
    if any(k in t for k in ["video", "videoclip", "youtube", "clip"]):
        return "videos"
    if any(k in t for k in ["festival", "parque de la leyenda", "rey vallenato"]):
        return "festivales"
    if any(k in t for k in ["podcast", "episodio", "capítulo", "capitulo"]):
        return "podcasts"
    if any(k in t for k in ["historia", "homenaje", "aniversario", "biografía", "biografia"]):
        return "historia"
    return "artistas"

def tags_from_text(text: str) -> list[str]:
    tags = ["vallenato"]
    low = text.lower()
    if "silvestre" in low:
        tags.append("Silvestre Dangond")
    if "diomedes" in low:
        tags.append("Diomedes Diaz")
    if "binomio" in low:
        tags.append("binomio de oro")
    if len(tags) < 2:
        tags.append("secundarios")

    out, seen = [], set()
    for x in tags:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out[:6]


# -------------------------
# OpenAI generation
# -------------------------
def openai_generate(article_text: str, source_url: str, title_hint: str, snippet_hint: str) -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("Falta OPENAI_API_KEY")

    system = (
        "Eres editor de un medio vallenato llamado Amalaya. "
        "Redactas notas originales, informativas y verificables. "
        "NO inventes datos. NO copies frases largas del texto fuente. "
        "Si algo no está confirmado, dilo explícitamente."
    )

    user = f"""
Fuente (URL): {source_url}

Título sugerido: {title_hint}
Snippet sugerido: {snippet_hint}

Texto fuente (solo para extraer hechos; NO copiar literalmente):
\"\"\"{article_text[:12000]}\"\"\"

Tarea:
- Escribe una noticia NUEVA para Amalaya basada en hechos verificables del texto fuente.
- Devuelve: title, seo (máx 160 caracteres), paragraphs (lista).
- Mínimo {MIN_PARAGRAPHS} párrafos. Usa los párrafos necesarios (no número fijo).
- Estilo: neutral, claro, sin emojis.
- En el último párrafo incluye atribución: "Información basada en la fuente consultada."
Devuelve SOLO JSON válido:
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

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=60)
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
            raise RuntimeError("No pude parsear JSON IA: " + out_text[:300])
        obj = json.loads(m.group(0))

    title = clean_text(obj.get("title", ""))[:140] or (clean_text(title_hint)[:140] or "Noticia vallenata")
    seo = clean_text(obj.get("seo", ""))[:160] or (clean_text(snippet_hint)[:160] or "Actualidad del vallenato.")
    paragraphs = [clean_text(p) for p in obj.get("paragraphs", []) if clean_text(p)]

    while len(paragraphs) < MIN_PARAGRAPHS:
        paragraphs.append("Amalaya seguirá verificando información para ampliar cuando haya detalles confirmados.")

    return {"title": title, "seo": seo, "paragraphs": paragraphs}


def main():
    sources = load_json(SOURCES_PATH, {"sites": [], "max_posts_per_run": 10})
    state = load_json(STATE_PATH, {"seen_urls": []})

    seen = set(state.get("seen_urls", []))
    sites = sources.get("sites", [])
    maxn = int(sources.get("max_posts_per_run", POSTS_PER_RUN))

    candidates = collect_candidate_urls(sites, seen, per_site=30)
    if not candidates:
        print("No encontré URLs nuevas.")
        return

    picked = candidates[:maxn]

    for url in picked:
        try:
            html = get_article_html(url)
            title_hint, snippet_hint = get_article_title_snippet(html)
            article_text = extract_article_text(html)

            article = openai_generate(article_text, url, title_hint, snippet_hint)

            title = article["title"]
            seo = article["seo"]
            paragraphs = article["paragraphs"]

            full_text = " ".join([title, seo] + paragraphs)
            category = pick_category(full_text)
            if category not in ALLOWED_CATEGORIES:
                category = "artistas"
            tags = tags_from_text(full_text)

            # imagen opcional
            media_block = ""
            og_img = get_og_image(html)
            if og_img:
                try:
                    dl = download_image(og_img)
                    if dl:
                        img_bytes, ext = dl
                        media = upload_media_to_wp(img_bytes, ext, filename_hint=title)
                        media_block = render_image_block(media["id"], media["source_url"])
                except Exception:
                    media_block = ""

            parts = []
            parts.append(render_p1(title))
            parts.append(render_p1(seo))
            parts.append(render_p1(paragraphs[0]))
            if media_block:
                parts.append(media_block)
            for p in paragraphs[1:]:
                parts.append(render_p1(p))

            parts.append(render_p1(category))
            parts.append(render_p1(", ".join(tags)))
            parts.append(render_p1("#autopublicar"))

            content_html = "\n".join(parts)
            post = create_post_in_wp(title, content_html)
            print(f"OK: {post.get('id')} - {post.get('link')}")

            seen.add(url)
            time.sleep(2)

        except Exception as e:
            print(f"ERROR con {url}: {e}")
            time.sleep(1)

    state["seen_urls"] = list(seen)[-1200:]
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
