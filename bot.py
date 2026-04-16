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
HOURS_LIMIT = 72
HEADERS = {"User-Agent": "AmalayaBot/1.7"}

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

def upload_media(img_url, title):
    """Sube imagen a WP y retorna el objeto media completo (con id y source_url)."""
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

def p_block(text):
    """Genera un bloque Gutenberg de párrafo con clase p1."""
    return (
        '<!-- wp:paragraph {"className":"p1"} -->\n'
        f'<p class="p1">{text}</p>\n'
        '<!-- /wp:paragraph -->'
    )

def img_block(media_id, media_url, alt=""):
    """Genera un bloque Gutenberg de imagen."""
    return (
        f'<!-- wp:image {{"id":{media_id},"sizeSlug":"large"}} -->\n'
        f'<figure class="wp-block-image size-large">'
        f'<img src="{media_url}" alt="{alt}" class="wp-image-{media_id}"/>'
        f'</figure>\n'
        '<!-- /wp:image -->'
    )

def build_content(data, media, portada_tag):
    """
    Construye el content en formato Gutenberg blocks exacto que espera el plugin.
    Estructura de posición que el plugin lee:
      - antepenúltima línea/bloque: categoría (solo el valor, sin prefijo)
      - penúltima línea/bloque: etiquetas separadas por coma (sin prefijo)
      - último bloque: #autopublicar
    """
    title    = data.get("title", "").strip()
    seo      = data.get("seo", "").strip()
    p1       = data.get("p1", "").strip()
    p2       = data.get("p2", "").strip()
    p3       = data.get("p3", "").strip()
    p4       = data.get("p4", "").strip()
    alt      = data.get("img_alt", "").strip()
    category = data.get("category", "artistas").strip()
    tags_list = list(data.get("tags", []))

    # Etiqueta de portada al inicio de la lista
    if portada_tag:
        tags_list = [portada_tag] + [t for t in tags_list if t not in ("principal", "secundarios")]
    tags_str = ", ".join(tags_list)

    blocks = []

    # Titular
    blocks.append(p_block(title))

    # Resumen SEO
    blocks.append(p_block(seo))

    # Primer párrafo (lead)
    blocks.append(p_block(p1))

    # Imagen destacada (si se subió correctamente)
    if media and media.get("id") and media.get("source_url"):
        blocks.append(img_block(media["id"], media["source_url"], alt))

    # Párrafos 2, 3, 4
    blocks.append(p_block(p2))
    blocks.append(p_block(p3))
    if p4:
        blocks.append(p_block(p4))

    # Antepenúltimo: categoría (solo el valor)
    blocks.append(p_block(category))

    # Penúltimo: etiquetas (solo los valores, separados por coma)
    blocks.append(p_block(tags_str))

    # Último: #autopublicar
    blocks.append(p_block("#autopublicar"))

    return "\n\n".join(blocks)

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

    # ── FASE 1: recolectar candidatos ─────────────────────────────────────────
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
                })
            except:
                continue

    if not candidates:
        print("[INFO] No hay candidatos nuevos para este run.")
        return

    # ── FASE 2: ranking por relevancia ────────────────────────────────────────
    ranking_prompt = f"""Eres editor de Amalaya.com.co, sitio de cultura vallenata colombiana.
Tienes {len(candidates)} noticias candidatas. Ordénalas de mayor a menor relevancia periodística para la portada.

Noticias:
{json.dumps([{"index": i, "texto": c["text"][:400]} for i, c in enumerate(candidates)], ensure_ascii=False)}

Devuelve SOLO un JSON con:
- "ranking": lista de índices ordenados de mayor a menor relevancia (ej: [3, 0, 5, 1, 2])"""

    ranking_data = call_openai(ranking_prompt)
    if ranking_data and "ranking" in ranking_data:
        ordered_indices = ranking_data["ranking"]
    else:
        ordered_indices = list(range(len(candidates)))

    # Asegurar que no falte ningún índice
    present = set(ordered_indices)
    for i in range(len(candidates)):
        if i not in present:
            ordered_indices.append(i)

    # ── FASE 3: redactar y publicar ───────────────────────────────────────────
    count = 0
    portada_rank = 0

    for idx in ordered_indices:
        if count >= POSTS_PER_RUN:
            break

        c = candidates[idx]

        if portada_rank == 0:
            portada_tag = "principal"
        elif portada_rank in [1, 2]:
            portada_tag = "secundarios"
        else:
            portada_tag = None
        portada_rank += 1

        article_prompt = f"""Eres redactor de Amalaya.com.co, sitio de cultura vallenata colombiana.
Escribe una noticia original basada en este texto fuente:

{c['text']}

Devuelve SOLO un JSON con estos campos exactos (sin campos extra):
- "title": titular periodístico atractivo en español
- "seo": frase resumen SEO de 120-160 caracteres, sin emojis, con nombre del artista o tema
- "p1": primer párrafo — lead informativo (qué pasó, quién, cuándo, dónde)
- "p2": segundo párrafo — contexto, antecedentes, cifras o fechas relevantes
- "p3": tercer párrafo — reacciones, citas o detalles del anuncio
- "p4": cuarto párrafo — qué sigue: fechas, ticketing, lanzamiento, próximos shows
- "img_alt": texto alternativo corto y descriptivo para la imagen destacada
- "category": UNA de estas opciones exactas: {ALLOWED_CATS_STR}
- "tags": lista de 5 a 8 etiquetas en español, minúsculas"""

        data = call_openai(article_prompt)
        if not data:
            continue

        try:
            # Subir imagen
            media = upload_media(c["img_url"], data.get("title", "")) if c.get("img_url") else None

            # Construir content en formato Gutenberg exacto del plugin
            content = build_content(data, media, portada_tag)

            # Publicar como draft
            post_payload = {
                "title": data.get("title", "Sin título"),
                "content": content,
                "status": "draft",
            }
            if media and media.get("id"):
                post_payload["featured_media"] = media["id"]

            res_wp = requests.post(
                f"{WP_URL}/wp-json/wp/v2/posts",
                headers=wp_headers(),
                json=post_payload,
                timeout=30
            )

            if res_wp.status_code in [200, 201]:
                label = f"[{portada_tag.upper()}]" if portada_tag else "[noticia]"
                print(f"{label} {data.get('title')} → {data.get('category')} | tags: {data.get('tags')}")
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
