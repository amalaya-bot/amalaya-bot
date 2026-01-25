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
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini") # Actualizado a modelo estándar

POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "10"))
MIN_PARAGRAPHS = int(os.environ.get("MIN_PARAGRAPHS", "4"))

HEADERS = {"User-Agent": "AmalayaBot/1.0 (+https://amalaya.com.co)"}

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None

# Categorías definidas en Amalaya
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

def esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def normalize_category_name(name: str) -> str:
    for c in ALLOWED_CATEGORIES:
        if c.lower() == name.lower(): return c
    return "artistas"

# =========================
# JWT Auth & WP API
# =========================
def get_jwt_token() -> str:
    global JWT_TOKEN
    if JWT_TOKEN: return JWT_TOKEN
    url = f"{WP_URL}/wp-json/jwt-auth/v1/token"
    r = requests.post(url, json={"username": WP_USER, "password": WP_PASSWORD}, timeout=30)
    r.raise_for_status()
    JWT_TOKEN = r.json().get("token")
    return JWT_TOKEN

def wp_headers_json():
    return {**HEADERS, "Authorization": f"Bearer {get_jwt_token()}", "Content-Type": "application/json"}

def wp_headers_upload(filename: str):
    return {**HEADERS, "Authorization": f"Bearer {get_jwt_token()}", "Content-Disposition": f'attachment; filename="{filename}"'}

def ensure_category(name: str) -> int:
    name = normalize_category_name(name)
    endpoint = f"{WP_URL}/wp-json/wp/v2/categories"
    try:
        r = requests.get(endpoint, headers=wp_headers_json(), params={"search": name}, timeout=25)
        items = r.json() if r.status_code == 200 else []
        for it in items:
            if it.get("name").lower() == name.lower(): return int(it["id"])
        # Crear si no existe
        r = requests.post(endpoint, headers=wp_headers_json(), json={"name": name}, timeout=25)
        return int(r.json()["id"])
    except: return 1 # Default

def ensure_tag(name: str) -> int:
    endpoint = f"{WP_URL}/wp-json/wp/v2/tags"
    try:
        r = requests.get(endpoint, headers=wp_headers_json(), params={"search": name}, timeout=25)
        items = r.json() if r.status_code == 200 else []
        for it in items:
            if it.get("name").lower() == name.lower(): return int(it["id"])
        r = requests.post(endpoint, headers=wp_headers_json(), json={"name": name}, timeout=25)
        return int(r.json()["id"])
    except: return 0

# =========================
# Image Handling
# =========================
def get_og_image(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    if og: return og.get("content")
    return None

def upload_media_to_wp(img_url: str, title: str) -> dict | None:
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        ext = "jpg"
        if "png" in r.headers.get("Content-Type", ""): ext = "png"
        filename = f"{re.sub(r'[^a-z0-9]', '-', title.lower())[:40]}.{ext}"
        
        up = requests.post(f"{WP_URL}/wp-json/wp/v2/media", headers=wp_headers_upload(filename), data=r.content, timeout=35)
        up.raise_for_status()
        res = up.json()
        return {"id": int(res["id"]), "url": res.get("source_url")}
    except: return None

# =========================
# Content Scraper & IA
# =========================
def is_vallenato_related(text: str) -> bool:
    keywords = ["vallenato", "acordeón", "diomedes", "silvestre", "valledupar", "juglar", "parque de la leyenda"]
    return any(k in text.lower() for k in keywords)

def openai_generate(article_text: str, source_url: str) -> dict:
    prompt = f"Escribe una noticia para el medio Amalaya basada en esto: {article_text[:8000]}. Devuelve SOLO JSON: {{'title': '...', 'seo': '...', 'paragraphs': ['...', '...']}}"
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "messages": [{"role": "system", "content": "Eres editor de Amalaya, un medio vallenato."}, {"role": "user", "content": prompt}],
            "response_format": { "type": "json_object" }
        }
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
    
    for site in sources["sites"]:
        # (Lógica simplificada de recolección para el ejemplo)
        # Aquí iría tu función collect_candidate_urls...
        urls = [site] # Ejemplo simplificado
        
        for url in urls:
            if url in seen: continue
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text()
                
                if not is_vallenato_related(text):
                    seen.add(url)
                    continue

                # IA Generation
                article = openai_generate(text, url)
                
                # Taxonomies
                category_name = "artistas" # O lógica pick_category
                cat_id = ensure_category(category_name)
                tag_ids = [ensure_tag("vallenato"), ensure_tag("secundarios")]
                
                # Image
                media_data = None
                og_img = get_og_image(r.text)
                if og_img:
                    media_data = upload_media_to_wp(og_img, article["title"])

                # --- ESTRUCTURA PARA EL PLUGIN (CRUCIAL) ---
                content_lines = []
                content_lines.append(article["title"]) # Línea 0
                content_lines.append(article["seo"])   # Línea 1
                
                for p in article["paragraphs"]:
                    content_lines.append(p)
                
                if media_data:
                    content_lines.append(f'<img src="{media_data["url"]}" />')
                
                content_lines.append(category_name)         # Metadata: Categoría
                content_lines.append("vallenato, secundarios") # Metadata: Tags
                content_lines.append("#autopublicar")       # Metadata: Trigger
                
                full_content = "\n\n".join(content_lines)

                # Publicar Post
                payload = {
                    "title": article["title"],
                    "content": full_content,
                    "status": WP_POST_STATUS,
                    "categories": [cat_id],
                    "tags": tag_ids,
                }
                if media_data:
                    payload["featured_media"] = media_data["id"]

                requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=wp_headers_json(), json=payload).raise_for_status()
                
                print(f"OK: {article['title']}")
                seen.add(url)
                
            except Exception as e:
                print(f"Error en {url}: {e}")

    state["seen_urls"] = list(seen)[-2000:]
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
