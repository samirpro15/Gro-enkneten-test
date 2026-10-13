#!/usr/bin/env python3
"""
Orte vorab laden: schreibt places-<ort>.json fuer jede Stadt.
Die Seite laedt diese fertigen Dateien sofort, statt bei jedem Besucher
Overpass live abzufragen. Optional werden Foto-Namen (Places API New) ergaenzt.
Secret (optional): GOOGLE_PLACES_API_KEY
"""
import json, math, os, sys, time, datetime
import requests

TOWNS = {
    "grossenkneten": (52.9333, 8.3167),
    "wildeshausen": (52.8996, 8.4342),
    "oldenburg": (53.1435, 8.2142),
}
RADIUS = 50000
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
KEEP = {"name", "brand", "operator", "amenity", "shop", "office", "leisure", "tourism",
        "natural", "healthcare", "cuisine", "opening_hours", "website", "contact:website",
        "addr:street", "addr:housenumber", "addr:city", "addr:town", "addr:village", "addr:postcode"}
KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
PHOTO_LOOKUPS_PER_RUN = 40   # begrenzt die Google-Kosten
PHOTO_TOP = 80               # nur die naechsten Orte pro Stadt bekommen ein Foto


def query(lat, lng):
    a = f"(around:{RADIUS},{lat},{lng})"
    return (
        '[out:json][timeout:180];('
        'nwr[amenity~"^(cafe|restaurant|fast_food|bar|pub|ice_cream|biergarten|doctors|clinic|hospital|pharmacy|dentist|townhall|public_building|courthouse|post_office|police|community_centre|library|fire_station|theatre|cinema|arts_centre|bank|fuel|car_wash|veterinary|driving_school)$"]' + a + ';'
        'nwr[shop][name]' + a + ';nwr[office=government]' + a + ';'
        'nwr[leisure~"^(park|playground|sports_centre|swimming_pool|nature_reserve|fitness_centre|golf_course|miniature_golf|water_park|stadium|ice_rink)$"]' + a + ';'
        'nwr[tourism~"^(attraction|museum|viewpoint|zoo|picnic_site|theme_park|gallery)$"]' + a + ';'
        ');out center tags 4000;'
    )


def overpass(q):
    err = None
    for attempt in range(4):
        for url in OVERPASS:
            try:
                r = requests.post(url, data={"data": q}, timeout=200)
                if r.ok:
                    return r.json()
                err = f"{url}: {r.status_code}"
            except Exception as e:  # noqa
                err = f"{url}: {e}"
        time.sleep(30 * (attempt + 1))  # bei 429 (zu viele Anfragen) lange genug warten
    raise RuntimeError(err)


def dist(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def photo_name(name, lat, lng):
    r = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": KEY, "X-Goog-FieldMask": "places.photos",
                 "Referer": "https://ortslotse.de/"},
        json={"textQuery": name, "maxResultCount": 1,
              "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 300}}},
        timeout=20,
    )
    if r.status_code in (401, 403):
        raise PermissionError(r.text[:200])
    r.raise_for_status()
    p = (r.json().get("places") or [{}])[0].get("photos") or []
    return p[0]["name"] if p else ""


def main():
    budget = PHOTO_LOOKUPS_PER_RUN if KEY else 0
    failed = 0
    first = True
    for key, (lat, lng) in TOWNS.items():
        if not first:
            time.sleep(45)  # Pause zwischen den Staedten, damit die Server nicht blocken
        first = False
        path = f"places-{key}.json"
        old = {}
        try:
            for e in json.load(open(path, encoding="utf-8")).get("elements", []):
                if "photo" in e:
                    old[f"{e['type']}{e['id']}"] = e["photo"]
        except Exception:
            pass
        try:
            data = overpass(query(lat, lng))
        except Exception as e:
            print(f"{key}: Overpass fehlgeschlagen ({e}), alte Datei bleibt", file=sys.stderr)
            failed += 1
            continue
        els = []
        for e in data.get("elements", []):
            t = {k: v for k, v in (e.get("tags") or {}).items() if k in KEEP}
            la = e.get("lat", (e.get("center") or {}).get("lat"))
            lo = e.get("lon", (e.get("center") or {}).get("lon"))
            if la is None or lo is None:
                continue
            els.append({"type": e["type"], "id": e["id"], "lat": round(la, 5), "lon": round(lo, 5), "tags": t})
        named = sorted((e for e in els if e["tags"].get("name")), key=lambda e: dist(lat, lng, e["lat"], e["lon"]))
        for i, e in enumerate(named):
            k = f"{e['type']}{e['id']}"
            if k in old:
                e["photo"] = old[k]
            elif i < PHOTO_TOP and budget > 0:
                try:
                    e["photo"] = photo_name(e["tags"]["name"], e["lat"], e["lon"])
                    budget -= 1
                except PermissionError as ex:
                    print("Foto-Abfrage nicht erlaubt, ueberspringe Fotos:", ex, file=sys.stderr)
                    budget = 0
                except Exception as ex:  # noqa
                    print("Foto-Abfrage Fehler:", ex, file=sys.stderr)
        out = {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(), "center": [lat, lng],
               "radius_m": RADIUS, "elements": els}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"{key}: {len(els)} Orte gespeichert")
    if failed:
        print(f"Hinweis: {failed} Stadt/Staedte nicht aktualisiert, die alten Dateien bleiben bestehen.", file=sys.stderr)
    # Nur rot werden, wenn eine Stadt gar keine Datei hat
    missing = [k for k in TOWNS if not os.path.exists(f"places-{k}.json")]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
