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
WP_POST_STATUS = os.environ.get("WP_POST_STATUS", "draft")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "10"))
MIN_PARAGRAPHS = int(os.environ.get("MIN_PARAGRAPHS", "4"))

HEADERS = {"User-Agent": "AmalayaBot/1.1 (+https://amalaya.com.co)"}

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None

ALLOWED_CATEGORIES = ["Historia", "lanzamientos", "videos", "artistas", "festivales", "podcasts"]
BASE_TAGS = ["Diomedes Diaz", "Silvestre Dangond", "vallenato", "binomio de oro", "secundarios"]

# =========================
# Helpers
# =========================
def load_json(path, default):
    if not os.path.exists(path): return default
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def clean_text(s: str) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", s).strip()

def normalize_category_name(name: str) -> str:
    for c in ALLOWED_CATEGORIES:
        if c.lower() == name.lower(): return c
    return "artistas"

# =========================
# WP API & Auth
# =========================
def get_jwt_token() -> str:
    global JWT_TOKEN
    if JWT_TOKEN: return JWT_TOKEN
    url = f"{WP_URL}/wp-json/jwt-auth/v1/token"
    print(f"[*] Autenticando en WP: {WP_USER}")
    r = requests.post(url, json={"username": WP_USER, "password": WP_PASSWORD}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Error JWT {r.status_code}: {r.text}")
    JWT_TOKEN = r.json().get("token")
    return JWT_TOKEN

def wp_headers_json():
    return {**HEADERS, "Authorization": f"Bearer {get_jwt_token()}", "Content-Type": "application/json"}

def wp_headers_upload(filename: str):
    return {**HEADERS, "Authorization": f"Bearer {get_jwt_token()}", "Content-Disposition": f'attachment; filename="{filename}"'}

def ensure_category(name: str) -> int:
    name = normalize_category_name(name)
    try:
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/categories", headers=wp_headers_json(), params={"search": name}, timeout=25)
        items = r.json() if r.status_code == 200 else []
        for it in items:
            if it.get("name").lower() == name.lower(): return int(it["id"])
        r = requests.post(f"{WP_URL}/wp-json/wp/v2/categories", headers=wp_headers_json(), json={"name": name}, timeout=25)
        return int(r.json()["id"])
    except: return 1

def ensure_tag(name: str) -> int:
    try:
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/tags", headers=wp_headers_json(), params={"search": name}, timeout=25)
        items = r.json() if r.status_code == 200 else []
        for it in items:
            if it.get("name").lower() == name.lower(): return int(it["id"])
        r = requests.post(f"{WP_URL}/wp-json/wp/v2/tags", headers=wp_headers_json(), json={"name": name}, timeout=25)
        return int(r.json()["id"])
    except: return 0

# =========================
# Scraper & Image
# =========================
def discover_feed(site_url: str) -> str | None:
    candidates = ["feed/", "rss", "feed.xml"]
    for c in candidates:
        url = urljoin(site_url, c)
        try:
            f = feedparser.parse(url)
            if len(f.entries) > 0: return url
        except: continue
    return None

def upload_media_to_wp(img_url: str, title: str) -> dict | None:
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        ext = "jpg"
        if "png" in r.headers.get("Content-Type", ""): ext = "png"
        filename = f"img-{int(time.time())}.{ext}"
        up = requests.post(f"{WP_URL}/wp-json/wp/v2/media", headers=wp_headers_upload(filename), data=r.content, timeout=35)
        res = up.json()
        return {"id": int(res["id"]), "url": res.get("source_url")}
    except: return None

# =========================
# Vallenato Filter & IA
# =========================
def is_vallenato_related(title: str, text: str) -> bool:
    t = (title + " " + text).lower()
    keywords = ["vallenato", "acordeón", "acordeon", "diomedes", "silvestre", "valledupar", "juglar", "binomio", "zuleta"]
    return any(k in t for k in keywords)

def openai_generate(article_text: str, source_url: str) -> dict:
    print(f"[*] Consultando IA para: {source_url[:50]}...")
    prompt = (
        f"Texto fuente: {article_text[:9000]}\n\n"
        "Tarea: Crea una noticia original. Devuelve SOLO un objeto JSON con esta estructura:\n"
        '{"title": "título llamativo", "seo": "resumen para Google", "paragraphs": ["párrafo 1", "párrafo 2", "párrafo 3", "párrafo 4"]}'
    )
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "Eres el editor jefe de Amalaya.com.co. Escribes en español neutro, tono periodístico."},
                {"role": "user", "content": prompt}
            ],
            "response_format": { "type": "json_object" }
        }, timeout=60
    )
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])

# =========================
# MAIN
# =========================
def main():
    sources = load_json(SOURCES_PATH, {"sites": []})
    state = load_json(STATE_PATH, {"seen_urls": []})
    seen = set(state["seen_urls"])
    
    sites = sources.get("sites", [])
    if not sites:
        print("[!] No hay sitios en sources.json")
        return

    # Recolectar URLs
    all_urls = []
    for s in sites:
        print(f"[*] Buscando noticias en: {s}")
        feed = discover_feed(s)
        if feed:
            f = feedparser.parse(feed)
            all_urls.extend([e.link for e in f.entries[:15]])
    
    new_urls = [u for u in all_urls if u not in seen][:POSTS_PER_RUN]
    print(f"[*] URLs nuevas encontradas: {len(new_urls)}")

    for url in new_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            soup = BeautifulSoup(r.text, "html.parser")
            
            # Extraer contenido básico
            title_hint = soup.title.string if soup.title else ""
            paras = [p.get_text() for p in soup.find_all('p') if len(p.get_text()) > 50]
            article_body = " ".join(paras)

            if not is_vallenato_related(title_hint, article_body):
                print(f"[SKIP] No parece vallenato: {url}")
                seen.add(url)
                continue

            # 1. Generar con IA
            data = openai_generate(article_body, url)
            
            # 2. Multimedia
            og_img = None
            meta_og = soup.find("meta", property="og:image")
            if meta_og: og_img = meta_og.get("content")
            
            media_info = upload_media_to_wp(og_img, data["title"]) if og_img else None

            # 3. Taxonomías
            category_name = "artistas" 
            cat_id = ensure_category(category_name)
            tag_ids = [ensure_tag("vallenato"), ensure_tag("secundarios")]

            # 4. Formato para el Plugin (Crucial para que no se vea el SEO ni el tag)
            # El plugin espera: Título, Excerpt, Párrafos..., Categoría, Tags, #autopublicar
            final_content = []
            final_content.append(data["title"]) # L0
            final_content.append(data["seo"])   # L1
            
            for p in data["paragraphs"]:
                final_content.append(p)
            
            if media_info:
                final_content.append(f'<img src="{media_info["url"]}" />')

            final_content.append(category_name)           # Penúltima - 2
            final_content.append("vallenato, artistas")   # Penúltima - 1
            final_content.append("#autopublicar")         # Última

            # Enviamos el contenido unido por saltos simples para que PHP lo detecte
            payload = {
                "title": data["title"],
                "content": "\n\n".join(final_content),
                "status": WP_POST_STATUS,
                "categories": [cat_id],
                "tags": tag_ids
            }
            if media_info:
                payload["featured_media"] = media_info["id"]

            post_res = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=wp_headers_json(), json=payload)
            if post_res.status_code in [200, 201]:
                print(f"[OK] Publicado: {data['title']}")
                seen.add(url)
            else:
                print(f"[ERROR] WP API: {post_res.text}")

        except Exception as e:
            print(f"[!] Error procesando {url}: {e}")

    state["seen_urls"] = list(seen)[-1000:]
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
