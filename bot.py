import os
import re
import json
import time
import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# =========================
# CONFIGURACIÓN (Variables de Entorno)
# =========================
WP_URL = os.environ.get("WP_URL", "https://amalaya.com.co").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_PASSWORD = os.environ.get("WP_PASSWORD", "")  # Application Password
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

POSTS_PER_RUN = 5
HEADERS = {"User-Agent": "AmalayaBot/1.2"}

# Rutas de archivos locales
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BOT_DIR, "sources.json")
STATE_PATH = os.path.join(BOT_DIR, "state.json")

JWT_TOKEN = None

# =========================
# AUTENTICACIÓN Y WP API
# =========================
def get_jwt_token():
    global JWT_TOKEN
    if JWT_TOKEN: return JWT_TOKEN
    url = f"{WP_URL}/wp-json/jwt-auth/v1/token"
    try:
        r = requests.post(url, json={"username": WP_USER, "password": WP_PASSWORD}, timeout=30)
        r.raise_for_status()
        JWT_TOKEN = r.json().get("token")
        return JWT_TOKEN
    except Exception as e:
        print(f"Error de autenticación JWT: {e}")
        return None

def wp_headers():
    return {
        "Authorization": f"Bearer {get_jwt_token()}",
        "Content-Type": "application/json",
        "User-Agent": HEADERS["User-Agent"]
    }

def ensure_taxonomy(endpoint, name):
    """Asegura que exista una categoría o etiqueta y devuelve su ID."""
    try:
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/{endpoint}", headers=wp_headers(), params={"search": name})
        items = r.json() if r.status_code == 200 else []
        for it in items:
            if it.get("name").lower() == name.lower(): return it["id"]
        
        # Si no existe, crearla
        r = requests.post(f"{WP_URL}/wp-json/wp/v2/{endpoint}", headers=wp_headers(), json={"name": name})
        return r.json().get("id")
    except: return None

# =========================
# MANEJO DE IMÁGENES
# =========================
def upload_media_to_wp(img_url, title):
    """Descarga la imagen de la fuente y la sube a la biblioteca de medios de WP."""
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        
        ctype = r.headers.get("Content-Type", "").lower()
        ext = "jpg"
        if "png" in ctype: ext = "png"
        elif "webp" in ctype: ext = "webp"
        
        filename = f"amalaya-{int(time.time())}.{ext}"
        
        upload_headers = {
            "Authorization": f"Bearer {get_jwt_token()}",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": ctype or "image/jpeg"
        }
        
        up = requests.post(f"{WP_URL}/wp-json/wp/v2/media", headers=upload_headers, data=r.content, timeout=40)
        if up.status_code in [200, 201]:
            res = up.json()
            print(f"[*] Imagen subida: {res.get('source_url')}")
            return {"id": res["id"], "url": res["source_url"]}
        return None
    except Exception as e:
        print(f"[-] Fallo al subir imagen: {e}")
        return None

# =========================
# LÓGICA DE CONTENIDO E IA
# =========================
def is_vallenato_related(text):
    keywords = ["vallenato", "acordeón", "diomedes", "silvestre", "valledupar", "juglar", "parque de la leyenda", "egidio", "zuleta"]
    return any(k in text.lower() for k in keywords)

def generate_with_openai(raw_text):
    """Genera la noticia formateada mediante IA."""
    prompt = (
        f"Basándote en este texto: {raw_text[:8000]}\n\n"
        "Escribe una noticia periodística para Amalaya.com.co. "
        "Devuelve exclusivamente un JSON con: 'title', 'seo' (resumen corto), 'paragraphs' (lista de 4 párrafos), 'category' y 'tags' (lista)."
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": OPENAI_MODEL,
                "messages": [{"role": "system", "content": "Eres el editor jefe de Amalaya.com.co, un medio experto en vallenato."},
                             {"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            }, timeout=60
        )
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"[-] Error OpenAI: {e}")
        return None

# =========================
# PROCESO PRINCIPAL
# =========================
def main():
    if not os.path.exists(SOURCES_PATH):
        print("[-] Crea un archivo sources.json con {'sites': []}")
        return

    with open(SOURCES_PATH, "r") as f: sources = json.load(f)
    state = {"seen_urls": []}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f: state = json.load(f)
    
    seen = set(state["seen_urls"])
    
    for site_url in sources.get("sites", []):
        print(f"[*] Revisando: {site_url}")
        feed = feedparser.parse(urljoin(site_url, "feed/"))
        
        for entry in feed.entries[:10]:
            if entry.link in seen: continue
            
            try:
                r = requests.get(entry.link, headers=HEADERS, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                body_text = " ".join([p.get_text() for p in soup.find_all("p")])

                if not is_vallenato_related(body_text):
                    seen.add(entry.link)
                    continue

                # 1. Generar contenido
                data = generate_with_openai(body_text)
                if not data: continue

                # 2. Manejar Imagen
                og_img = None
                meta_og = soup.find("meta", property="og:image")
                if meta_og: og_img = meta_og.get("content")
                
                media = upload_media_to_wp(og_img, data["title"]) if og_img else None
                featured_id = media["id"] if media else None

                # 3. Taxonomías
                cat_id = ensure_taxonomy("categories", data.get("category", "artistas"))
                tag_ids = [ensure_taxonomy("tags", t) for t in data.get("tags", ["vallenato"])]

                # 4. Estructura de Texto Plano para el Plugin
                # Orden: Título, SEO, Párrafos..., Categoría, Tags, #autopublicar
                content_lines = [data["title"].strip(), data["seo"].strip()]
                content_lines.extend([p.strip() for p in data["paragraphs"]])
                content_lines.append(data.get("category", "artistas"))
                content_lines.append(", ".join(data.get("tags", ["vallenato"])))
                content_lines.append("#autopublicar")

                # 5. Publicar como Borrador (El plugin hará el resto)
                post_payload = {
                    "title": data["title"],
                    "content": "\n".join(content_lines),
                    "status": "draft",
                    "categories": [cat_id] if cat_id else [],
                    "tags": [tid for tid in tag_ids if tid],
                    "featured_media": featured_id
                }

                res = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", headers=wp_headers(), json=post_payload)
                
                if res.status_code in [200, 201]:
                    print(f"[OK] Post creado: {data['title']}")
                    seen.add(entry.link)
                else:
                    print(f"[-] Error WP: {res.text}")

            except Exception as e:
                print(f"[-] Error procesando {entry.link}: {e}")

    # Guardar estado
    state["seen_urls"] = list(seen)[-1000:]
    with open(STATE_PATH, "w") as f: json.dump(state, f)

if __name__ == "__main__":
    main()
