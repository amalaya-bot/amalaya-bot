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
HEADERS = {"User-Agent": "AmalayaBot/1.5"}

# Categorías disponibles en el sitio (slugs exactos de WordPress)
ALLOWED_CATS = ["historia", "lanzamientos", "videos", "artistas", "festivales", "podcasts", "cronicas"]
ALLOWED_CATS_STR = ", ".join(ALLOWED_CATS)

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
    except:
        return None

def wp_headers():
    return {"Authorization": f"Bearer {get_jwt_token()}", "Content-Type": "application/json"}

def get_cat_id(slug):
    """Busca el ID de una categoría por su slug en WordPress."""
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"slug": slug},
            headers=wp_headers(),
            timeout=15
        )
        results = r.json()
        if results:
            return results[0]["id"]
    except:
        pass
    # Fallback: buscar "artistas"
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"slug": "artistas"},
            headers=wp_headers(),
            timeout=15
        )
        results = r.json()
        if results:
            return results[0]["id"]
    except:
        pass
    return None

def get_valid_cat_slug(suggested_name):
    """Normaliza el nombre sugerido por la IA al slug más cercano permitido."""
    name = suggested_name.lower().strip()
    # Mapa de variantes posibles que la IA podría devolver
    alias_map = {
        "crónicas": "cronicas",
        "crónica": "cronicas",
        "cronica": "cronicas",
        "podcast": "podcasts",
        "video": "videos",
        "festival": "festivales",
        "artista": "artistas",
        "lanzamiento": "lanzamientos",
    }
    name = alias_map.get(name, name)
    return name if name in ALLOWED_CATS else "artistas"

def get_or_create_tag(tag_name):
    """Retorna el ID de un tag en WordPress, creándolo si no existe."""
    headers = wp_headers()
    name = tag_name.lower().strip()
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/tags",
            params={"search": name},
            headers=headers,
            timeout=15
        )
        results = r.json()
        if results:
            # Buscar coincidencia exacta primero
            for t in results:
                if t["name"].lower() == name:
                    return t["id"]
            # Si no hay exacta, usar la primera
            return results[0]["id"]
        # Crear el tag
        r2 = requests.post(
            f"{WP_URL}/wp-json/wp/v2/tags",
            headers=headers,
            json={"name": name},
            timeout=15
        )
        if r2.status_code in [200, 201]:
            return r2.json()["id"]
    except:
        pass
    return None

def resolve_tag_ids(tag_names):
    """Convierte lista de nombres de tags a lista de IDs de WordPress."""
    ids = []
    for name in tag_names:
        tid = get_or_create_tag(name)
        if tid:
            ids.append(tid)
    return ids

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
    except:
        return None

def call_openai(prompt):
    """Llama a OpenAI y retorna el JSON parseado, o None si falla."""
    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            },
            timeout=60
        )
        return json.loads(res.json()["choices"][0]["message"]["content"])
    except:
        return None

# =========================
# PROCESO PRINCIPAL
# =========================
def main():
    if not os.path.exists(SOURCES_PATH):
        return
    with open(SOURCES_PATH, "r") as f:
        sources = json.load(f)

    state = {"seen_urls": []}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
    seen = set(state["seen_urls"])

    # ── FASE 1: recolectar candidatos ──────────────────────────────────────────
    candidates = []

    for site in sources.get("sites", []):
        if len(candidates) >= POSTS_PER_RUN * 2:
            break
        feed = feedparser.parse(urljoin(site, "feed/"))

        for entry in feed.entries:
            if len(candidates) >= POSTS_PER_RUN * 2:
                break
            if entry.link in seen:
                continue
            if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
                continue

            pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
            if datetime.now() - pub_date > timedelta(hours=HOURS_LIMIT):
                continue

            try:
                r = requests.get(entry.link, headers=HEADERS, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                text = " ".join([p.get_text() for p in soup.find_all("p")])
                og = soup.find("meta", property="og:image")
                img_url = og.get("content") if og else None

                candidates.append({
                    "url": entry.link,
                    "text": text[:6000],
                    "img_url": img_url,
                    "pub_date": pub_date.isoformat()
                })
            except:
                continue

    if not candidates:
        print("[INFO] No hay candidatos nuevos para este run.")
        return

    # ── FASE 2: pedir a la IA que ordene por relevancia ───────────────────────
    ranking_prompt = f"""Eres editor de Amalaya.com.co, sitio de cultura vallenata colombiana.
A continuación tienes {len(candidates)} noticias candidatas numeradas del 0 al {len(candidates)-1}.
Ordénalas de mayor a menor relevancia periodística para la portada del sitio.

Noticias:
{json.dumps([{"index": i, "texto": c["text"][:500]} for i, c in enumerate(candidates)], ensure_ascii=False)}

Devuelve SOLO un JSON con este campo:
- "ranking": lista de índices ordenados de mayor a menor relevancia (ej: [3, 0, 5, 1, 2])"""

    ranking_data = call_openai(ranking_prompt)
    if ranking_data and "ranking" in ranking_data:
        ordered_indices = ranking_data["ranking"]
    else:
        # Si la IA falla el ranking, usar orden original
        ordered_indices = list(range(len(candidates)))

    # Asegurarse de que todos los índices estén presentes (por si la IA omite alguno)
    present = set(ordered_indices)
    for i in range(len(candidates)):
        if i not in present:
            ordered_indices.append(i)

    # ── FASE 3: procesar y publicar en orden de relevancia ────────────────────
    count = 0
    portada_rank = 0  # 0 = principal, 1 y 2 = secundarios, resto = sin etiqueta de portada

    for idx in ordered_indices:
        if count >= POSTS_PER_RUN:
            break

        c = candidates[idx]

        # Etiqueta de portada según posición
        if portada_rank == 0:
            portada_tag = "principal"
        elif portada_rank in [1, 2]:
            portada_tag = "secundarios"
        else:
            portada_tag = None
        portada_rank += 1

        # Prompt de redacción
        article_prompt = f"""Eres redactor de Amalaya.com.co, sitio de cultura vallenata colombiana.
Escribe una noticia original basada en el siguiente texto fuente:

{c['text']}

Devuelve SOLO un JSON con estos campos:
- "title": título atractivo y periodístico en español
- "seo": descripción SEO de máximo 155 caracteres
- "paragraphs": lista de exactamente 4 párrafos bien redactados
- "category": UNA de estas opciones exactas (elige la más apropiada): {ALLOWED_CATS_STR}
- "tags": lista de 5 a 8 etiquetas relevantes en español, en minúsculas, sin tildes obligatorias

No uses ninguna categoría fuera de la lista. No agregues campos extra."""

        data = call_openai(article_prompt)
        if not data:
            continue

        try:
            # Subir imagen
            media = upload_media(c["img_url"], data["title"]) if c.get("img_url") else None

            # Resolver categoría
            cat_slug = get_valid_cat_slug(data.get("category", "artistas"))
            cat_id = get_cat_id(cat_slug)

            # Resolver etiquetas: temáticas + portada
            tag_names = [t.lower().strip() for t in data.get("tags", [])]
            if portada_tag:
                tag_names = [portada_tag] + tag_names
            tag_ids = resolve_tag_ids(tag_names)

            # Contenido: solo los párrafos (el plugin maneja el resto)
            content = "\n\n".join(data["paragraphs"]) + "\n#autopublicar"

            # Publicar como draft
            post_payload = {
                "title": data["title"],
                "content": content,
                "excerpt": data.get("seo", ""),
                "status": "draft",
                "featured_media": media["id"] if media else None,
                "tags": tag_ids
            }
            if cat_id:
                post_payload["categories"] = [cat_id]

            res_wp = requests.post(
                f"{WP_URL}/wp-json/wp/v2/posts",
                headers=wp_headers(),
                json=post_payload,
                timeout=30
            )

            if res_wp.status_code in [200, 201]:
                label = f"[{portada_tag.upper()}]" if portada_tag else "[noticia]"
                print(f"{label} {data['title']} → {cat_slug} | tags: {tag_names}")
                seen.add(c["url"])
                count += 1
            else:
                print(f"[ERROR WP] {res_wp.status_code} - {res_wp.text[:200]}")

        except Exception as e:
            print(f"[ERROR] {e}")
            continue

    # ── Guardar estado ─────────────────────────────────────────────────────────
    state["seen_urls"] = list(seen)[-1000:]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"[FIN] {count} posts publicados en este run.")

if __name__ == "__main__":
    main()
