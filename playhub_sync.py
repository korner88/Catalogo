import json, requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TOKEN = "582592|sh1GGaGirWhqYrFIHJzZ4XWuq76tUPxIrG1jtEeW374ca8c7"
BASE_URL = "https://api-admin.playhub-plus.com"
HEADERS = {"accept": "application/json", "authorization": f"Bearer {TOKEN}", "user-agent": "NukePlay", "x-platform": "android"}

def obtener_uuids(content_type):
    todas, page, has_more = [], 1, True
    while has_more:
        try:
            res = requests.get(f"{BASE_URL}/contents?contentType={content_type}&page={page}", headers=HEADERS, timeout=10)
            if res.status_code == 200:
                data = res.json()
                todas.extend([item['uuid'] for item in data.get("data", [])])
                has_more = data.get("hasMore", False)
                page += 1
            else: break
        except: break
    return todas

def obtener_tmdb(uuid, session):
    try:
        res = session.get(f"{BASE_URL}/contents/{uuid}", timeout=10)
        if res.status_code == 200: return uuid, res.json().get("tmdbId")
    except: pass
    return uuid, None

def procesar(content_type, output_file):
    uuids = obtener_uuids(content_type)
    mapa, session = {}, requests.Session()
    session.headers.update(HEADERS)
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(obtener_tmdb, u, session): u for u in uuids}
        for future in as_completed(futures):
            uuid, tmdb_id = future.result()
            if tmdb_id: mapa[str(tmdb_id)] = uuid
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(mapa, f, indent=4)

if __name__ == "__main__":
    procesar("Movie", "mapa_peliculas_playhub.json")
    procesar("Show", "mapa_series_playhub.json")
