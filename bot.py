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
HEADERS = {"User-Agent": "AmalayaBot/1.8"}

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

def find_image_url(soup, entry):
    """
    Intenta obtener una URL de imagen válida con múltiples estrategias:
    1. og:image del artículo
    2. Primera imagen <img> con src que termine en extensión de imagen
    3. Enclosure del feed entry
    Retorna la URL si la encuentra, None si no hay imagen.
    """
    # Estrategia 1: og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content", "").strip():
        url = og["content"].strip()
        if any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            return url
        # Aunque no tenga extensión clara, si tiene og:image la intentamos
        return url

    # Estrategia 2: primera <img> grande en el cuerpo
    for img in soup.find_all("img"):
        src = img.get("src", "") or img.get("data-src", "") or img.get("data-lazy-src", "")
        if not src:
            continue
        if any(src.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            # Ignorar iconos o imágenes pequeñas por nombre
            if any(skip in src.lower() for skip in ["logo", "icon", "avatar", "pixel", "spacer"]):
                continue
            return src

    # Estrategia 3: enclosure del feed
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image/"):
                return enc.get("href") or enc.get("url")

    return None

def upload_media(img_url, title):
    """Sube imagen a WP y retorna el objeto media completo (con id y source_url)."""
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("Content-Type", "").lower()
        if not ctype.startswith("image/"):
            return None
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
    - antepenúltimo bloque: categoría (solo el valor)
    - penúltimo bloque: etiquetas separadas por coma (solo los valores)
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

    if portada_tag:
        tags_list = [portada_tag] + [t for t in tags_list if t not in ("principal", "secundarios")]
    tags_str = ", ".join(tags_list)

    blocks = [
        p_block(title),
        p_block(seo),
        p_block(p1),
        img_block(media["id"], media["source_url"], alt),
        p_block(p2),
        p_block(p3),
    ]
    if p4:
        blocks.append(p_block(p4))

    # Posición fija que lee el plugin
    blocks.append(p_block(category))
    blocks.append(p_block(tags_str))
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

    # ── FASE 1: recolectar candidatos con imagen ───────────────────────────────
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

                # Buscar imagen con múltiples estrategias
                img_url = find_image_url(soup, entry)

                # Descartar si no hay imagen
                if not img_url:
                    print(f"[SKIP sin imagen] {entry.link}")
                    seen.add(entry.link)  # marcar para no revisitar
                    continue

                candidates.append({
                    "url": entry.link,
                    "text": text[:6000],
                    "img_url": img_url,
                })
            except:
                continue

    if not candidates:
        print("[INFO] No hay candidatos nuevos para este run.")
        state["seen_urls"] = list(seen)[-1000:]
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
        return

    # ── FASE 2: ranking + deduplicación por tema ──────────────────────────────
    ranking_prompt = f"""Eres editor de Amalaya.com.co, sitio de cultura vallenata colombiana.
Tienes {len(candidates)} noticias candidatas. Tu tarea es:
1. Identificar noticias que cubran el MISMO tema o hecho (aunque vengan de distintos sitios) y quedarte solo con la mejor de cada grupo.
2. Ordenar las noticias únicas de mayor a menor relevancia periodística para la portada.

Noticias:
{json.dumps([{"index": i, "texto": c["text"][:400]} for i, c in enumerate(candidates)], ensure_ascii=False)}

Devuelve SOLO un JSON con:
- "ranking": lista de índices únicos ordenados de mayor a menor relevancia. Si dos noticias son del mismo tema, incluye solo el índice de la mejor y omite el otro."""

    ranking_data = call_openai(ranking_prompt)
    if ranking_data and "ranking" in ranking_data:
        ordered_indices = ranking_data["ranking"]
    else:
        ordered_indices = list(range(len(candidates)))

    # Asegurar que no haya índices fuera de rango
    ordered_indices = [i for i in ordered_indices if 0 <= i < len(candidates)]

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
            portada_rank -= 1  # no consumir posición de portada si falló la IA
            continue

        try:
            # Subir imagen — descartar si falla
            media = upload_media(c["img_url"], data.get("title", ""))
            if not media or not media.get("id") or not media.get("source_url"):
                print(f"[SKIP imagen no subió] {c['url']}")
                seen.add(c["url"])
                portada_rank -= 1  # no consumir posición de portada
                continue

            # Construir content en formato Gutenberg exacto del plugin
            content = build_content(data, media, portada_tag)

            # Publicar como draft
            post_payload = {
                "title": data.get("title", "Sin título"),
                "content": content,
                "status": "draft",
                "featured_media": media["id"],
            }

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
                portada_rank -= 1

        except Exception as e:
            print(f"[ERROR] {e}")
            portada_rank -= 1
            continue

    # ── Guardar estado ─────────────────────────────────────────────────────────
    state["seen_urls"] = list(seen)[-1000:]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"[FIN] {count} posts publicados en este run.")

if __name__ == "__main__":
    main()
