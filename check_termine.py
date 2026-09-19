#!/usr/bin/env python3
"""
Terminradar Großenkneten
=========================
Prüft die offizielle Online-Terminvergabe der Gemeinde Großenkneten
(https://termine.crossing.de/4878781/Appointment/Index/1) auf freie
Behördentermine für ALLE dort wählbaren Anliegen und meldet neu
aufgetauchte freie Termine per ntfy.sh-Push-Benachrichtigung.

WICHTIG: Dieses Skript bucht nichts automatisch. Es liest nur die
öffentlich sichtbare Terminübersicht und benachrichtigt dich. Die
eigentliche Buchung machst du weiterhin selbst auf der offiziellen
Seite.

Ablauf:
1. Seite mit der Anliegen-Auswahl laden.
2. Formular (Checkboxen + versteckte Felder) automatisch erkennen und
   ALLE Anliegen auswählen.
3. Formular absenden und die resultierende Kalender-/Terminansicht
   nach freien Terminen durchsuchen.
4. Ergebnis mit dem letzten bekannten Stand (status.json) vergleichen.
5. Bei neu aufgetauchten freien Terminen: Push über ntfy.sh senden.
6. status.json aktualisieren (wird von der GitHub Action committet und
   von der Status-Webseite (index.html) angezeigt).

Wenn sich die Struktur der Webseite ändert und das Skript die
Anliegen oder den Kalender nicht mehr zuverlässig erkennt, wird das
NICHT stillschweigend als "keine Termine frei" gewertet, sondern als
technischer Fehler gemeldet (status "error"), inkl. einer
Push-Benachrichtigung, damit so ein Problem auffällt statt unbemerkt
falsche Ruhe vorzutäuschen.
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
# Konfiguration
# ---------------------------------------------------------------------------

BASE_HOST = "https://termine.crossing.de"
ORG_ID = "4878781"  # Gemeinde Großenkneten
INDEX_URL = f"{BASE_HOST}/{ORG_ID}/Appointment/Index/1"

STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")
DEBUG_HTML_FILE = "debug_last_response.html"

# Der ntfy-Themenname kommt aus einer Umgebungsvariable (GitHub Actions
# Secret). Lokal zum Testen kannst du ihn auch direkt hier eintragen.
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


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Status speichern/laden, Benachrichtigungen
# ---------------------------------------------------------------------------

def load_previous_status() -> dict:
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"status": "unknown", "available": [], "last_checked": None, "message": ""}


def save_status(data: dict) -> None:
    data["last_checked"] = datetime.now(timezone.utc).isoformat()
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"status.json geschrieben: {data}")


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
# Scraping-Logik
# ---------------------------------------------------------------------------

def resolve_url(action: str | None) -> str:
    if not action:
        return INDEX_URL
    if action.startswith("http"):
        return action
    if action.startswith("/"):
        return BASE_HOST + action
    return f"{BASE_HOST}/{ORG_ID}/" + action.lstrip("/")


def load_concern_form(session: requests.Session):
    """Lädt die Anliegen-Auswahlseite und gibt (action_url, payload, anzahl_checkboxen) zurück."""
    resp = session.get(INDEX_URL, headers=REQUEST_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()

    # Rohantwort der Anliegen-Seite IMMER sichern (auch wenn wir gleich
    # einen Fehler werfen), damit wir bei Bedarf sehen können, wie die
    # Seite tatsächlich aufgebaut ist.
    with open(DEBUG_HTML_FILE, "w", encoding="utf-8") as f:
        f.write(resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")

    form = soup.find("form")
    if form is None:
        raise RuntimeError("Kein <form> auf der Anliegen-Seite gefunden.")

    action_url = resolve_url(form.get("action"))

    payload: dict[str, str] = {}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        if name:
            payload[name] = hidden.get("value", "")

    checkboxes = form.find_all("input", {"type": "checkbox"})
    for box in checkboxes:
        name = box.get("name")
        if name:
            payload[name] = box.get("value", "true")

    return action_url, payload, len(checkboxes)


def find_available_dates(html: str) -> list[str]:
    """
    Sucht in der Kalender-/Terminansicht nach Hinweisen auf freie Termine.

    Diese Funktion ist bewusst großzügig geschrieben (mehrere Heuristiken),
    weil der genaue Aufbau der Kalenderseite ohne Live-Zugriff auf die
    Seite (inkl. JavaScript) nicht zu 100% vorherbestimmbar war. Falls sie
    hier nachjustiert werden muss, ist das der einzige Ort, an dem das
    nötig ist.
    """
    soup = BeautifulSoup(html, "html.parser")
    dates: set[str] = set()

    date_pattern = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{2,4})\b")

    # Heuristik 1: anklickbare Elemente (Links/Buttons), die NICHT als
    # "disabled"/"belegt"/"ausgebucht" markiert sind und ein Datum im
    # Text oder in einem data-* Attribut tragen.
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
        match = date_pattern.search(text)
        if match:
            dates.add(match.group(1))
            continue

        for attr in ("data-date", "data-day", "title", "aria-label"):
            if el.has_attr(attr):
                match = date_pattern.search(el[attr])
                if match:
                    dates.add(match.group(1))

    # Heuristik 2: eingebettete JSON-/JS-Datenstrukturen mit Datumsangaben
    # (manche Systeme laden den Kalender über ein <script>-Objekt).
    for script in soup.find_all("script"):
        content = script.string or ""
        if "available" in content.lower() or "frei" in content.lower():
            for match in date_pattern.finditer(content):
                dates.add(match.group(1))

    return sorted(dates)


def explicit_no_appointments(html: str) -> bool:
    """Erkennt gängige Formulierungen für 'keine freien Termine'."""
    lowered = html.lower()
    phrases = [
        "keine freien termine",
        "leider keine termine",
        "keine verfügbaren termine",
        "aktuell keine termine",
        "es sind keine termine",
    ]
    return any(p in lowered for p in phrases)


def check_grossenkneten() -> dict:
    session = requests.Session()
    action_url, payload, n_checkboxes = load_concern_form(session)

    if n_checkboxes == 0:
        raise RuntimeError(
            "Keine Anliegen-Checkboxen gefunden. Die Seite wählt Anliegen "
            "vermutlich per JavaScript aus, nicht über ein normales "
            "HTML-Formular. -> Skript muss nachjustiert werden."
        )

    resp = session.post(action_url, data=payload, headers=REQUEST_HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()

    # Rohantwort für die manuelle Kalibrierung sichern (wird nur bei
    # Bedarf ausgewertet, kostet aber nichts).
    with open(DEBUG_HTML_FILE, "w", encoding="utf-8") as f:
        f.write(resp.text)

    if explicit_no_appointments(resp.text):
        return {"status": "ok", "available": []}

    dates = find_available_dates(resp.text)
    return {"status": "ok", "available": dates}


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    previous = load_previous_status()

    try:
        result = check_grossenkneten()
    except Exception as exc:  # noqa: BLE001 - wir wollen jeden Fehler abfangen und melden
        error_text = f"{exc}"
        print("Fehler beim Prüfen:", error_text, file=sys.stderr)
        traceback.print_exc()

        new_status = {
            "status": "error",
            "available": previous.get("available", []),
            "message": error_text,
        }
        save_status(new_status)

        if previous.get("status") != "error":
            notify(
                "Terminradar: technischer Fehler",
                (
                    "Die Terminseite von Großenkneten konnte nicht ausgewertet werden "
                    f"({error_text}). Bitte kurz manuell prüfen: {INDEX_URL}"
                ),
                priority="high",
                tags="warning",
            )
        return

    available = result["available"]
    previous_available = set(previous.get("available", []))
    new_dates = sorted(set(available) - previous_available)

    if new_dates and previous.get("status") != "error":
        notify(
            "Neuer freier Termin in Großenkneten!",
            "Neu verfügbar: " + ", ".join(new_dates) + f"\nJetzt buchen: {INDEX_URL}",
            priority="urgent",
            tags="tada,calendar",
        )
    elif new_dates and previous.get("status") == "error":
        # Nach einer Fehlerphase erst mal nur informieren, keine "urgent"-Eskalation
        notify(
            "Terminradar läuft wieder",
            "Freie Termine gefunden: " + ", ".join(new_dates) + f"\n{INDEX_URL}",
            priority="default",
        )

    save_status(
        {
            "status": "ok",
            "available": available,
            "message": "",
        }
    )


if __name__ == "__main__":
    main()
