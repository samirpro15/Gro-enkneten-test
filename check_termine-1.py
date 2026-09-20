#!/usr/bin/env python3
"""
Terminradar (Mehrere Gemeinden)
=================================
Prüft mehrere offizielle Online-Terminvergaben auf freie Behördentermine
und meldet neu aufgetauchte freie Termine per ntfy.sh-Push-Benachrichtigung.

Aktuell überwacht:
  - Gemeinde Großenkneten   (System: crossing.de)
  - Stadt Wildeshausen      (System: TEVIS / Kommunix)
  - Stadt Oldenburg         (System: TEVIS / Kommunix)

WICHTIG: Dieses Skript bucht nichts automatisch. Es liest nur die
öffentlich sichtbare Terminübersicht und benachrichtigt dich. Die
eigentliche Buchung machst du weiterhin selbst auf der offiziellen Seite.

Jede Gemeinde wird UNABHÄNGIG geprüft: schlägt eine Seite fehl (z. B.
weil sich ihre Struktur geändert hat), betrifft das nicht die anderen.
Für jede Gemeinde wird bei Bedarf eine eigene Debug-HTML-Datei als
GitHub-Actions-Artifact gespeichert (debug_<key>.html), damit sich das
Skript bei Bedarf gezielt nachjustieren lässt.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Allgemeine Konfiguration
# ---------------------------------------------------------------------------

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

TIMEOUT = 30
DATE_PATTERN = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{2,4})\b")


def debug_file_for(key: str) -> str:
    return f"debug_{key}.html"


def save_debug(key: str, html: str) -> None:
    try:
        with open(debug_file_for(key), "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as exc:
        print(f"[{key}] Konnte Debug-Datei nicht schreiben: {exc}", file=sys.stderr)


def explicit_no_appointments(html: str) -> bool:
    lowered = html.lower()
    phrases = [
        "keine freien termine",
        "leider keine termine",
        "keine verfügbaren termine",
        "aktuell keine termine",
        "es sind keine termine",
        "derzeit keine freien termine",
        "keine termine verfügbar",
    ]
    return any(p in lowered for p in phrases)


def find_available_dates(html: str) -> list[str]:
    """Großzügige Heuristik für Kalender-/Terminübersichtsseiten."""
    soup = BeautifulSoup(html, "html.parser")
    dates: set[str] = set()

    for el in soup.find_all(["a", "button", "td", "div"]):
        classes = " ".join(el.get("class", [])).lower()
        is_disabled = el.has_attr("disabled") or any(
            word in classes
            for word in ("disabled", "inactive", "belegt", "ausgebucht", "unavailable", "past")
        )
        looks_clickable = el.name in ("a", "button") or any(
            word in classes for word in ("available", "free", "frei", "bookable", "buchbar", "active", "selectable")
        )
        if is_disabled or not looks_clickable:
            continue

        text = el.get_text(" ", strip=True)
        match = DATE_PATTERN.search(text)
        if match:
            dates.add(match.group(1))
            continue

        for attr in ("data-date", "data-day", "title", "aria-label"):
            if el.has_attr(attr):
                match = DATE_PATTERN.search(el[attr])
                if match:
                    dates.add(match.group(1))

    for script in soup.find_all("script"):
        content = script.string or ""
        if "available" in content.lower() or "frei" in content.lower():
            for match in DATE_PATTERN.finditer(content):
                dates.add(match.group(1))

    return sorted(dates)


# ---------------------------------------------------------------------------
# Gemeinde 1: Großenkneten (System: crossing.de)
# ---------------------------------------------------------------------------

def check_grossenkneten() -> dict:
    base_host = "https://termine.crossing.de"
    org_id = "4878781"
    index_url = f"{base_host}/{org_id}/Appointment/Index/1"

    session = requests.Session()

    resp = session.get(index_url, headers=REQUEST_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    save_debug("grossenkneten", resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", {"id": "form-add-concern-items"}) or soup.find("form")
    if form is None:
        raise RuntimeError("Kein <form> auf der Anliegen-Seite gefunden.")

    action = form.get("action") or index_url
    if action.startswith("http"):
        action_url = action
    elif action.startswith("/"):
        action_url = base_host + action
    else:
        action_url = f"{base_host}/{org_id}/" + action.lstrip("/")

    payload: dict[str, str] = {}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        if name:
            payload[name] = hidden.get("value", "")

    concern_inputs = form.find_all("input", {"name": re.compile(r"^concern-\d+$")})
    for box in concern_inputs:
        name = box.get("name")
        if name:
            payload[name] = "1"

    if not concern_inputs:
        raise RuntimeError(
            "Keine Anliegen-Mengenfelder (concern-<ID>) gefunden. Die Struktur "
            "der Seite hat sich vermutlich erneut geändert."
        )

    resp2 = session.post(action_url, data=payload, headers=REQUEST_HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp2.raise_for_status()

    calendar_url = f"{base_host}/{org_id}/Appointment/SelectDateAndTime"
    cal_resp = session.get(calendar_url, headers=REQUEST_HEADERS, timeout=TIMEOUT, allow_redirects=True)
    cal_resp.raise_for_status()
    save_debug("grossenkneten", cal_resp.text)

    if explicit_no_appointments(cal_resp.text):
        return {"available": []}
    return {"available": find_available_dates(cal_resp.text)}


# ---------------------------------------------------------------------------
# Gemeinden auf dem TEVIS-System (Wildeshausen, Oldenburg)
# ---------------------------------------------------------------------------
#
# TEVIS ist ein mehrstufiger Assistent (Schritt 1-6: Behörde -> Anliegen ->
# Termin -> persönliche Daten -> Übersicht -> Bestätigung). Diese erste
# Version versucht die Anliegen-Felder generisch zu erkennen; falls das
# (noch) nicht zuverlässig klappt, wird das als "technischer Fehler"
# gemeldet und die rohe Seite als Debug-Artifact gespeichert, damit die
# Logik gezielt nachjustiert werden kann - genau wie zuvor bei
# Großenkneten.

def check_tevis(key: str, select2_url: str) -> dict:
    session = requests.Session()

    resp = session.get(select2_url, headers=REQUEST_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    save_debug(key, resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("Kein <form> auf der Anliegen-Seite gefunden (Schritt 2 von 6).")

    candidate_inputs = form.find_all("input", {"type": re.compile(r"^(number|text|checkbox|radio)$")})
    concern_like = [
        inp for inp in candidate_inputs
        if inp.get("name") and re.search(r"concern|anliegen|item|leistung", inp.get("name", ""), re.I)
    ]

    if not concern_like:
        raise RuntimeError(
            "Konnte die Anliegen-Auswahlfelder auf der TEVIS-Seite (Schritt 2 "
            "von 6) noch nicht automatisch erkennen - vermutlich läuft die "
            "Auswahl komplett über JavaScript/AJAX-Aufrufe statt über normale "
            "Formularfelder. Braucht eine gezielte Nachjustierung anhand der "
            f"gespeicherten Debug-Datei ({debug_file_for(key)})."
        )

    payload: dict[str, str] = {}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        if name:
            payload[name] = hidden.get("value", "")
    for inp in concern_like:
        name = inp.get("name")
        if inp.get("type") in ("checkbox", "radio"):
            payload[name] = inp.get("value", "true")
        else:
            payload[name] = "1"

    action = form.get("action") or select2_url
    if not action.startswith("http"):
        from urllib.parse import urljoin
        action = urljoin(select2_url, action)

    resp2 = session.post(action, data=payload, headers=REQUEST_HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp2.raise_for_status()
    save_debug(key, resp2.text)

    if explicit_no_appointments(resp2.text):
        return {"available": []}
    return {"available": find_available_dates(resp2.text)}


def check_wildeshausen() -> dict:
    return check_tevis("wildeshausen", "https://onlinetermine.wildeshausen.de/select2?md=1")


def check_oldenburg() -> dict:
    return check_tevis("oldenburg", "https://terminvereinbarung.oldenburg.de/select2?md=1")


# ---------------------------------------------------------------------------
# Zu überwachende Gemeinden
# ---------------------------------------------------------------------------

SITES = [
    {
        "key": "grossenkneten",
        "label": "Großenkneten",
        "url": "https://termine.crossing.de/4878781/Appointment/Index/1",
        "check": check_grossenkneten,
    },
    {
        "key": "wildeshausen",
        "label": "Wildeshausen",
        "url": "https://onlinetermine.wildeshausen.de/select2?md=1",
        "check": check_wildeshausen,
    },
    {
        "key": "oldenburg",
        "label": "Oldenburg",
        "url": "https://terminvereinbarung.oldenburg.de/select2?md=1",
        "check": check_oldenburg,
    },
]


# ---------------------------------------------------------------------------
# Status speichern/laden, Benachrichtigungen
# ---------------------------------------------------------------------------

def load_previous_status() -> dict:
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "sites" in data:
                    return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"sites": {}, "last_checked": None}


def notify(title: str, message: str, priority: str = "default", tags: str = "calendar") -> None:
    if not NTFY_URL:
        print("Kein NTFY_TOPIC gesetzt – überspringe Push-Benachrichtigung.", file=sys.stderr)
        return
    try:
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": tags,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"Push-Benachrichtigung fehlgeschlagen: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    previous = load_previous_status()
    previous_sites = previous.get("sites", {})

    new_sites: dict[str, dict] = {}

    for site in SITES:
        key = site["key"]
        label = site["label"]
        url = site["url"]
        prev_site = previous_sites.get(key, {"status": "unknown", "available": []})

        try:
            result = site["check"]()
            available = result["available"]

            previous_available = set(prev_site.get("available", []))
            new_dates = sorted(set(available) - previous_available)

            if new_dates and prev_site.get("status") != "error":
                notify(
                    f"Neuer freier Termin in {label}!",
                    "Neu verfügbar: " + ", ".join(new_dates) + f"\nJetzt buchen: {url}",
                    priority="urgent",
                    tags="tada,calendar",
                )
            elif new_dates and prev_site.get("status") == "error":
                notify(
                    f"Terminradar {label} läuft wieder",
                    "Freie Termine gefunden: " + ", ".join(new_dates) + f"\n{url}",
                    priority="default",
                )

            new_sites[key] = {
                "label": label,
                "url": url,
                "status": "ok",
                "available": available,
                "message": "",
            }
            print(f"[{key}] OK - {len(available)} Termin(e) gefunden.")

        except Exception as exc:  # noqa: BLE001
            error_text = f"{exc}"
            print(f"[{key}] Fehler beim Prüfen:", error_text, file=sys.stderr)
            traceback.print_exc()

            new_sites[key] = {
                "label": label,
                "url": url,
                "status": "error",
                "available": prev_site.get("available", []),
                "message": error_text,
            }

            if prev_site.get("status") != "error":
                notify(
                    f"Terminradar {label}: technischer Fehler",
                    f"Die Terminseite von {label} konnte nicht ausgewertet werden "
                    f"({error_text}). Bitte kurz manuell prüfen: {url}",
                    priority="high",
                    tags="warning",
                )

    status_data = {
        "sites": new_sites,
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_data, f, ensure_ascii=False, indent=2)
    print("status.json geschrieben:", json.dumps(status_data, ensure_ascii=False))


if __name__ == "__main__":
    main()
