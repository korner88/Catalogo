import difflib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

TOKEN = os.environ.get("PLAYHUB_TOKEN", "").strip()
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "1200"))

BASE_URL = "https://api-admin.playhub-plus.com"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
CACHE_FILE = "playhub_cache.json"
MAX_RETRIES_UNMATCHED = 3
MAX_WORKERS = 8

HEADERS = {
    "accept": "application/json",
    "authorization": f"Bearer {TOKEN}",
    "user-agent": "NukePlay",
    "x-platform": "android",
}

_thread_local = threading.local()
_cache_lock = threading.Lock()
_person_lock = threading.Lock()
_person_credits_ram = {}


def get_playhub_session():
    sess = getattr(_thread_local, "playhub_session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        _thread_local.playhub_session = sess
    return sess


def get_tmdb_session():
    sess = getattr(_thread_local, "tmdb_session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"accept": "application/json"})
        # Soporta tanto API Key v3 (32 chars) como Read Access Token v4 (JWT eyJ...)
        if TMDB_API_KEY.startswith("eyJ") or len(TMDB_API_KEY) > 40:
            sess.headers["authorization"] = f"Bearer {TMDB_API_KEY}"
        _thread_local.tmdb_session = sess
    return sess


def tmdb_get(path, params=None):
    if not TMDB_API_KEY:
        return None
    sess = get_tmdb_session()
    q = dict(params or {})
    if "authorization" not in sess.headers:
        q["api_key"] = TMDB_API_KEY

    for intento in range(3):
        try:
            res = sess.get(f"{TMDB_BASE_URL}{path}", params=q, timeout=12)
            if res.status_code == 200:
                return res.json()
            if res.status_code == 429:
                wait = float(res.headers.get("Retry-After", "2"))
                time.sleep(min(wait, 10))
                continue
            return None
        except Exception:
            time.sleep(1 + intento)
    return None


def normalizar(texto):
    if not texto:
        return ""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", sin_acentos.lower()).strip()


def puntuar_candidato(cand, norm_title, release_date, year, is_movie):
    cand_date = (cand.get("release_date") if is_movie else cand.get("first_air_date")) or ""
    cand_year = cand_date[:4] if len(cand_date) >= 4 else ""

    t1 = normalizar(cand.get("title") if is_movie else cand.get("name"))
    t2 = normalizar(cand.get("original_title") if is_movie else cand.get("original_name"))

    score = 0
    if release_date and cand_date == release_date:
        score += 60
    elif year and cand_year:
        if cand_year == year:
            score += 25
        elif year.isdigit() and cand_year.isdigit() and abs(int(cand_year) - int(year)) == 1:
            score += 10

    if norm_title:
        if norm_title in (t1, t2):
            score += 50
        else:
            sim = max(
                difflib.SequenceMatcher(None, norm_title, t1).ratio() if t1 else 0.0,
                difflib.SequenceMatcher(None, norm_title, t2).ratio() if t2 else 0.0,
            )
            if sim >= 0.85:
                score += 35
            elif sim >= 0.70:
                score += 20

    return score


def obtener_creditos_persona(person_tmdb_id, is_movie):
    key = (str(person_tmdb_id), "movie" if is_movie else "tv")
    with _person_lock:
        if key in _person_credits_ram:
            return _person_credits_ram[key]

    endpoint = f"/person/{person_tmdb_id}/{'movie_credits' if is_movie else 'tv_credits'}"
    data = tmdb_get(endpoint)
    cast = data.get("cast", []) if data else []

    with _person_lock:
        _person_credits_ram[key] = cast
    return cast


def resolver_tmdb_desde_detalles(details, content_type):
    # 1. Por si algún ítem todavía incluye tmdbId directo en la raíz
    directo = details.get("tmdbId")
    if directo:
        return str(directo)

    if not TMDB_API_KEY:
        return None

    is_movie = (details.get("type") or content_type).lower() == "movie"
    title = (details.get("title") or "").strip()
    norm_title = normalizar(title)
    release_date = (details.get("releaseDate") or "").strip()[:10]
    year = release_date[:4] if len(release_date) >= 4 else ""

    # 2. Match por créditos de actores (people[].tmdbId) + intersección de elenco
    people_ids = []
    for p in details.get("people") or []:
        pid = p.get("tmdbId")
        if pid and pid not in people_ids:
            people_ids.append(pid)
        if len(people_ids) >= 3:
            break

    if people_ids:
        candidatos = {}
        conteo_actores = {}

        for pid in people_ids:
            for c in obtener_creditos_persona(pid, is_movie):
                cid = c.get("id")
                if not cid:
                    continue
                candidatos[cid] = c
                conteo_actores[cid] = conteo_actores.get(cid, 0) + 1

        mejor_id, mejor_score = None, 0
        for cid, cand in candidatos.items():
            base_score = puntuar_candidato(cand, norm_title, release_date, year, is_movie)
            if base_score < 20:
                continue
            extra_actores = conteo_actores.get(cid, 1) - 1
            score = base_score + (50 * extra_actores if extra_actores > 0 else 0)
            if score > mejor_score:
                mejor_score = score
                mejor_id = cid

        if mejor_id and mejor_score >= 75:
            return str(mejor_id)

    # 3. Fallback por búsqueda de título + año
    if title:
        search_path = "/search/movie" if is_movie else "/search/tv"
        year_param = "year" if is_movie else "first_air_date_year"

        resultados = []
        if year:
            r = tmdb_get(search_path, {"query": title, year_param: year})
            resultados = r.get("results", []) if r else []
        if not resultados:
            r = tmdb_get(search_path, {"query": title})
            resultados = r.get("results", []) if r else []

        mejor_id, mejor_score = None, 0
        for cand in resultados[:10]:
            score = puntuar_candidato(cand, norm_title, release_date, year, is_movie)
            if score > mejor_score:
                mejor_score = score
                mejor_id = cand.get("id")

        if mejor_id and mejor_score >= 70:
            return str(mejor_id)

    return None


def obtener_uuids(content_type):
    todas, page, has_more = [], 1, True
    sess = get_playhub_session()
    while has_more:
        ok = False
        for intento in range(3):
            try:
                res = sess.get(
                    f"{BASE_URL}/contents?contentType={content_type}&page={page}",
                    timeout=15,
                )
                if res.status_code == 200:
                    data = res.json()
                    todas.extend(
                        item["uuid"] for item in data.get("data", []) if item.get("uuid")
                    )
                    has_more = data.get("hasMore", False)
                    page += 1
                    ok = True
                    break
                if res.status_code == 429:
                    time.sleep(2 + intento * 2)
                    continue
                print(f"[{content_type}] HTTP {res.status_code} en página {page}")
                break
            except Exception as e:
                print(f"[{content_type}] Error en página {page}: {e}")
                time.sleep(1 + intento)
        if not ok:
            break
    return todas


def consultar_detalle_y_resolver(uuid, content_type):
    sess = get_playhub_session()
    for intento in range(3):
        try:
            res = sess.get(f"{BASE_URL}/contents/{uuid}", timeout=15)
            if res.status_code == 200:
                tmdb_id = resolver_tmdb_desde_detalles(res.json(), content_type)
                return uuid, tmdb_id, True
            if res.status_code == 429:
                time.sleep(2 + intento * 2)
                continue
            return uuid, None, False
        except Exception:
            time.sleep(1 + intento)
    return uuid, None, False


def cargar_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def guardar_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def procesar(content_type, output_file, cache, cuota_restante):
    uuids = obtener_uuids(content_type)
    print(f"[{content_type}] UUIDs activos en PlayHub: {len(uuids)}")

    if not uuids:
        print(f"[{content_type}] Lista vacía (posible error de red/token). Se conserva {output_file}.")
        return cuota_restante

    pendientes = []
    for u in uuids:
        entry = cache.get(u)
        if entry is None:
            pendientes.append(u)
        elif not entry.get("tmdb_id") and entry.get("retries", 0) < MAX_RETRIES_UNMATCHED:
            pendientes.append(u)

    if MAX_NEW_PER_RUN > 0 and len(pendientes) > cuota_restante:
        print(f"[{content_type}] Pendientes: {len(pendientes)} -> procesando lote de {cuota_restante}")
        pendientes = pendientes[:cuota_restante]
    else:
        print(f"[{content_type}] Pendientes por resolver: {len(pendientes)}")

    resueltos_ahora = 0
    if pendientes:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(consultar_detalle_y_resolver, u, content_type): u
                for u in pendientes
            }
            for future in as_completed(futures):
                uuid, tmdb_id, http_ok = future.result()
                with _cache_lock:
                    prev = cache.get(uuid) or {}
                    if tmdb_id:
                        cache[uuid] = {"tmdb_id": str(tmdb_id), "type": content_type}
                        resueltos_ahora += 1
                    elif http_ok:
                        cache[uuid] = {
                            "tmdb_id": None,
                            "type": content_type,
                            "retries": prev.get("retries", 0) + 1,
                        }

    mapa = {}
    conteo_tids = {}
    for u in uuids:
        tid = (cache.get(u) or {}).get("tmdb_id")
        if tid:
            tid_str = str(tid)
            conteo_tids[tid_str] = conteo_tids.get(tid_str, 0) + 1
            mapa[tid_str] = u

    colisiones = {k: v for k, v in conteo_tids.items() if v > 1}
    print(f"[{content_type}] Nuevos resueltos: {resueltos_ahora} | Total en mapa: {len(mapa)}")
    if colisiones:
        sobrescritos = sum(v - 1 for v in colisiones.values())
        top_col = sorted(colisiones.items(), key=lambda x: x[1], reverse=True)[:10]
        print(f"[{content_type}] AVISO: {sobrescritos} duplicados sobrescritos -> top (tmdb_id, veces): {top_col}")

    if mapa:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(mapa, f, indent=4)

    return max(0, cuota_restante - len(pendientes))


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: Falta la variable de entorno PLAYHUB_TOKEN.")
        sys.exit(1)
    if not TMDB_API_KEY:
        print("AVISO: TMDB_API_KEY no configurada; solo se mapearán ítems con tmdbId nativo.")

    cache = cargar_cache()
    if os.environ.get("RESET_SHOW_CACHE") == "1":
        antes = len(cache)
        cache = {u: v for u, v in cache.items() if v.get("type") != "Show"}
        print(f"RESET_SHOW_CACHE: eliminadas {antes - len(cache)} entradas de Show (Movie intactas: {len(cache)})")
    cuota = MAX_NEW_PER_RUN if MAX_NEW_PER_RUN > 0 else 10**9

    procesar("Movie", "mapa_peliculas_playhub.json", cache, cuota)
    guardar_cache(cache)

    procesar("Show", "mapa_series_playhub.json", cache, cuota)
    guardar_cache(cache)
