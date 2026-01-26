import os
import json
import time
import requests
import feedparser
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# =========================
# CONFIGURACIÓN
# =========================
WP_URL = os.environ.get("WP_URL", "https://amalaya.com.co").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_PASSWORD = os.environ.get("WP_PASSWORD", "") 
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

POSTS_PER_RUN = 10
HOURS_LIMIT = 24
HEADERS = {"User-Agent": "AmalayaBot/1.4"}

# CATEGORÍAS PERMITIDAS (Ajusta los nombres según tu WP)
ALLOWED_CATS = ["artistas", "lanzamientos", "videos", "festivales", "historia", "opinion", "cronicas"]

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

# =========================
# FUNCIONES DE APOYO
# =========================
def get_jwt_token():
    url = f"{WP_URL}/wp-json/jwt-auth/v1/token"
    try:
        r = requests.post(url, json={"username": WP_USER, "password": WP_PASSWORD}, timeout=30)
        return r.json().get("token")
    except: return None

def wp_headers():
    return {"Authorization": f"Bearer {get_jwt_token()}", "Content-Type": "application/json"}

def get_valid_cat(suggested_name):
    """Verifica si la categoría sugerida existe, si no, usa 'artistas'."""
    name = suggested_name.lower().strip()
    if name in ALLOWED_CATS:
        return name
    return "artistas"

def upload_media(img_url, title):
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=25)
        ctype = r.headers.get("Content-Type", "").lower()
        filename = f"amalaya-{int(time.time())}.jpg"
        h = {
            "Authorization": f"Bearer {get_jwt_token()}",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": ctype or "image/jpeg"
        }
        up = requests.post(f"{WP_URL}/wp-json/wp/v2/media", headers=h, data=r.content, timeout=40)
        return up.json() if up.status_code in [200, 201] else None
    except: return None

# =========================
# PROCESO PRINCIPAL
# =========================
def main():
    if not os.path.exists(SOURCES_PATH): return
    with open(SOURCES_PATH, "r") as f: sources = json.load(f)
    
    state = {"seen_urls": []}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f: state = json.load(f)
    seen = set(state["seen_urls"])
    
    count = 0
    for site in sources.get("sites", []):
        if count >= POSTS_PER_RUN: break
        feed = feedparser.parse(urljoin(site, "feed/"))
        
        for entry in feed.entries:
            if count >= POSTS_PER_RUN or entry.link in seen: continue
            
            # Filtro 24h
            pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
            if datetime.now() - pub_date > timedelta(hours=HOURS_LIMIT): continue

            try:
                r = requests.get(entry.link, headers=HEADERS, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                text = " ".join([p.get_text() for p in soup.find_all("p")])
                
                # IA
                prompt = f"Escribe una noticia para Amalaya.com.co sobre: {text[:6000]}. Devuelve JSON: 'title', 'seo', 'paragraphs' (lista de 4), 'category'."
                res_ai = requests.post("https://api.openai.com/v1/chat/completions", 
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    json={"model": OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}})
                data = json.loads(res_ai.json()["choices"][0]["message"]["content"])

                # Imagen
                og = soup.find("meta", property="og:image")
                media = upload_media(og.get("content"), data["title"]) if og else None
                
                # Metadata limpia para el plugin
                cat = get_valid_cat(data.get("category", "artistas"))
                content = f"{data['title']}\n{data['seo']}\n" + "\n".join(data["paragraphs"]) + f"\n{cat}\nvallenato, artistas\n#autopublicar"

                # Post
                res_wp = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=wp_headers(), 
                    json={"title": data["title"], "content": content, "status": "draft", "featured_media": media["id"] if media else None})
                
                if res_wp.status_code in [200, 201]:
                    print(f"[OK] {data['title']}")
                    seen.add(entry.link)
                    count += 1
            except: continue

    state["seen_urls"] = list(seen)[-1000:]
    with open(STATE_PATH, "w") as f: json.dump(state, f)

if __name__ == "__main__": main()
