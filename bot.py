#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
from email.utils import parsedate_to_datetime

import requests
import feedparser
from bs4 import BeautifulSoup

# =========================
# ENV
# =========================
WP_URL = os.environ.get("WP_URL", "https://amalaya.com.co").rstrip("/")
WP_USER = os.environ.get("WP_USER")
WP_PASSWORD = os.environ.get("WP_PASSWORD")
WP_POST_STATUS = os.environ.get("WP_POST_STATUS", "draft")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "10"))
MIN_PARAGRAPHS = int(os.environ.get("MIN_PARAGRAPHS", "4"))

HEADERS = {
    "User-Agent": "AmalayaBot/1.1 (+https://amalaya.com.co)"
}

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None

# =========================
# UTILS
# =========================
def clean_text(s: str) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", s).strip()

def esc_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# =========================
# JWT AUTH
# =========================
def get_jwt_token():
    global JWT_TOKEN
    if JWT_TOKEN:
        return JWT_TOKEN

    r = requests.post(
        f"{WP_URL}/wp-json/jwt-auth/v1/token",
        json={"username": WP_USER, "password": WP_PASSWORD},
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    r.raise_for_status()
    JWT_TOKEN = r.json()["token"]
    return JWT_TOKEN

def wp_headers_json():
    return {
        **HEADERS,
        "Authorization": f"Bearer {get_jwt_token()}",
        "Content-Type": "application/json"
    }

def wp_headers_upload(filename, content_type):
    return {
        **HEADERS,
        "Authorization": f"Bearer {get_jwt_token()}",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": content_type
    }

# =========================
# TIME FILTER (24H)
# =========================
def is_within_last_24h(dt):
    if not dt:
        return False
    now = datetime.now(timezone.utc)
    return dt >= (now - timedelta(hours=24))

# =========================
# VALLENATO FILTER
# =========================
def is_vallenato(text: str) -> bool:
    t = text.lower()

    must = [
        "vallenato", "acordeón", "acordeon", "guacharaca", "caja",
        "diomedes", "silvestre", "binomio", "poncho zuleta",
        "martín elías", "martin elias", "rafa pérez", "rafa perez",
        "festival vallenato", "valledupar", "parque de la leyenda"
    ]

    blocked = [
        "política", "politica", "judicial", "elecciones",
        "registraduría", "registraduria", "economía",
        "fútbol", "futbol", "internacional"
    ]

    if any(b in t for b in blocked) and not any(m in t for m in must):
        return False

    return any(m in t for m in must)

# =========================
# IMAGE HANDLING
# =========================
def get_best_image_url(html, base_url):
    soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"]

    main = soup.find("article") or soup.find("main") or soup.body
    if main:
        img = main.find("img")
        if img:
            src = img.get("src") or img.get("data-src")
            if src:
                return urljoin(base_url, src)

    return None

def download_image(url, referer):
    r = requests.get(
        url,
        headers={**HEADERS, "Referer": referer},
        timeout=30,
        allow_redirects=True
    )
    r.raise_for_status()

    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    ext = "jpg"
    if "png" in ctype:
        ext = "png"
    elif "webp" in ctype:
        ext = "webp"

    return r.content, ext, ctype

def upload_media(img_bytes, ext, ctype, title):
    filename = re.sub(r"[^a-zA-Z0-9_-]+", "-", title.lower())[:40] + "." + ext
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/media",
        headers=wp_headers_upload(filename, ctype),
        data=img_bytes,
        timeout=40
    )
    r.raise_for_status()
    j = r.json()
    return j["id"], j.get("source_url")

# =========================
# OPENAI
# =========================
def generate_article(source_text, url, title_hint):
    system = (
        "Eres editor de un medio vallenato llamado Amalaya. "
        "Redactas noticias originales, neutrales y verificables. "
        "NO inventes datos ni copies frases largas."
    )

    user = f"""
Fuente: {url}

Texto base:
\"\"\"{source_text[:12000]}\"\"\"

Devuelve SOLO JSON:
{{
  "title": "...",
  "seo": "...",
  "paragraphs": ["...", "..."]
}}
"""

    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": OPENAI_MODEL,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "max_output_tokens": 900
        },
        timeout=80
    )
    r.raise_for_status()

    out = ""
    for item in r.json().get("output", []):
        for c in item.get("content", []):
            if c.get("type") in ("output_text", "text"):
                out += c.get("text", "")

    obj = json.loads(re.search(r"\{.*\}", out, re.S).group(0))

    return obj

# =========================
# MAIN
# =========================
def main():
    sources = load_json(SOURCES_PATH, {"sites": []})
    state = load_json(STATE_PATH, {"seen": []})
    seen = set(state["seen"])

    published = 0

    for site in sources["sites"]:
        if published >= POSTS_PER_RUN:
            break

        feed = feedparser.parse(site)
        for e in feed.entries:
            if published >= POSTS_PER_RUN:
                break

            url = getattr(e, "link", None)
            if not url or url in seen:
                continue

            pub = None
            if hasattr(e, "published"):
                pub = parsedate_to_datetime(e.published)

            if pub and not is_within_last_24h(pub):
                continue

            html = requests.get(url, headers=HEADERS, timeout=30).text
            text = clean_text(BeautifulSoup(html, "html.parser").get_text(" "))

            if not is_vallenato(text):
                continue

            article = generate_article(text, url, "")
            title = article["title"]
            seo = article["seo"]
            paragraphs = article["paragraphs"]

            featured_id = None
            img_url = get_best_image_url(html, url)
            if img_url:
                try:
                    img_bytes, ext, ctype = download_image(img_url, url)
                    featured_id, _ = upload_media(img_bytes, ext, ctype, title)
                except Exception:
                    pass

            content = []
            content.append(f'<p class="p1">{esc_html(title)}</p>')
            content.append(f'<p class="p1">{esc_html(paragraphs[0])}</p>')
            for p in paragraphs[1:]:
                content.append(f'<p class="p1">{esc_html(p)}</p>')
            content.append('<p class="amalaya-meta">#autopublicar</p>')

            payload = {
                "title": title,
                "content": "\n".join(content),
                "excerpt": seo,
                "status": WP_POST_STATUS
            }
            if featured_id:
                payload["featured_media"] = featured_id

            r = requests.post(
                f"{WP_URL}/wp-json/wp/v2/posts",
                headers=wp_headers_json(),
                json=payload,
                timeout=40
            )
            r.raise_for_status()

            print("OK:", url)
            seen.add(url)
            published += 1
            time.sleep(2)

    state["seen"] = list(seen)[-2000:]
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
