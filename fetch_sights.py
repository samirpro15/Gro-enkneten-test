#!/usr/bin/env python3
"""
Sehenswürdigkeiten & Freizeit – Datenabruf über die Google Places API
=======================================================================
Ruft für jede überwachte Stadt Sehenswürdigkeiten, Freizeitaktivitäten,
Cafés/Restaurants usw. über die Google Places API (Nearby Search) ab und
speichert das Ergebnis in sights.json.

Läuft NICHT alle 15 Minuten wie check_termine.py (Sehenswürdigkeiten
ändern sich praktisch nie), sondern über einen eigenen, selten laufenden
Workflow (z. B. einmal pro Woche oder manuell).

Benötigt das GitHub-Secret GOOGLE_PLACES_API_KEY.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

import requests

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
SIGHTS_FILE = "sights.json"

# Der Referer muss zur HTTP-Verweis-URL-Einschränkung des API-Keys passen
# (siehe Einrichtung), sonst lehnt Google die Anfrage ab, obwohl der Key
# gültig ist.
REQUEST_HEADERS = {"Referer": "https://samirpro15.github.io/"}

NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PHOTO_BASE_URL = "https://maps.googleapis.com/maps/api/place/photo"
RADIUS_METERS = 6000

# Für jede Stadt: Mittelpunkt-Koordinaten (grobe Stadtmitte reicht).
TOWNS = {
    "grossenkneten": {"label": "Großenkneten", "lat": 52.9333, "lng": 8.3167},
    "wildeshausen": {"label": "Wildeshausen", "lat": 52.8996, "lng": 8.4342},
    "oldenburg": {"label": "Oldenburg", "lat": 53.1435, "lng": 8.2146},
}

# Google-"type" pro Suchlauf -> unsere eigene Kategorie. Mehrere Läufe pro
# Stadt, weil die Nearby-Search-API nur einen "type" pro Anfrage erlaubt.
TYPE_TO_CATEGORY = {
    "tourist_attraction": "Kultur",
    "museum": "Kultur",
    "church": "Kultur",
    "art_gallery": "Kultur",
    "park": "Natur",
    "natural_feature": "Natur",
    "cafe": "Gastro",
    "restaurant": "Gastro",
    "bakery": "Gastro",
    "gym": "Sport",
    "stadium": "Sport",
}


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def photo_url(photo_reference: str, maxwidth: int = 480) -> str:
    return f"{PHOTO_BASE_URL}?maxwidth={maxwidth}&photoreference={photo_reference}&key={API_KEY}"


def fetch_places_for_type(lat: float, lng: float, place_type: str) -> list[dict]:
    params = {
        "location": f"{lat},{lng}",
        "radius": RADIUS_METERS,
        "type": place_type,
        "key": API_KEY,
    }
    resp = requests.get(NEARBY_URL, params=params, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    if status not in ("OK", "ZERO_RESULTS"):
        raise RuntimeError(f"Places API Fehler ({place_type}): {status} – {data.get('error_message', '')}")
    return data.get("results", [])


def fetch_town_sights(town_key: str, town: dict) -> list[dict]:
    seen_place_ids: dict[str, dict] = {}

    for place_type, category in TYPE_TO_CATEGORY.items():
        try:
            results = fetch_places_for_type(town["lat"], town["lng"], place_type)
        except RuntimeError as exc:
            print(f"[{town_key}] Warnung bei Typ '{place_type}': {exc}", file=sys.stderr)
            continue

        for r in results:
            place_id = r.get("place_id")
            if not place_id or place_id in seen_place_ids:
                continue
            if r.get("business_status") not in (None, "OPERATIONAL"):
                continue

            loc = r.get("geometry", {}).get("location", {})
            distance_km = None
            if loc.get("lat") is not None and loc.get("lng") is not None:
                distance_km = round(haversine_km(town["lat"], town["lng"], loc["lat"], loc["lng"]), 1)

            photos = r.get("photos", [])
            photo = photo_url(photos[0]["photo_reference"]) if photos else None

            seen_place_ids[place_id] = {
                "name": r.get("name"),
                "category": category,
                "rating": r.get("rating"),
                "user_ratings_total": r.get("user_ratings_total"),
                "address": r.get("vicinity"),
                "distance_km": distance_km,
                "photo_url": photo,
                "place_id": place_id,
            }

        time.sleep(0.2)  # kleine Pause, um die API nicht zu hämmern

    places = list(seen_place_ids.values())
    # Beste zuerst: höhere Bewertung, dann mehr Bewertungen.
    places.sort(key=lambda p: (p["rating"] or 0, p["user_ratings_total"] or 0), reverse=True)
    return places


def main() -> None:
    if not API_KEY:
        print("GOOGLE_PLACES_API_KEY ist nicht gesetzt - breche ab.", file=sys.stderr)
        sys.exit(1)

    result: dict[str, list[dict]] = {}
    for town_key, town in TOWNS.items():
        print(f"Rufe Orte für {town['label']} ab ...")
        try:
            result[town_key] = fetch_town_sights(town_key, town)
            print(f"  -> {len(result[town_key])} Orte gefunden.")
        except Exception as exc:  # noqa: BLE001
            print(f"[{town_key}] Fehler: {exc}", file=sys.stderr)
            result[town_key] = []

    with open(SIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"{SIGHTS_FILE} geschrieben.")


if __name__ == "__main__":
    main()
