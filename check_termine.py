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
        return {"concerns": {"Alle Anliegen": {"status": "ok", "available": []}}}
    return {"concerns": {"Alle Anliegen": {"status": "ok", "available": find_available_dates(cal_resp.text)}}}


# ---------------------------------------------------------------------------
# Gemeinden auf dem TEVIS-System (Wildeshausen, Oldenburg)
# ---------------------------------------------------------------------------
#
# TEVIS ist ein mehrstufiger Assistent (Schritt 1-6: Behörde -> Anliegen ->
# ggf. Standort -> Termin -> persönliche Daten -> Bestätigung). Jedes
# Anliegen hat ein Mengen-Feld namens "cnc-<ID>" (Typ number) sowie
# optional zugehörige Dokument-Bestätigungs-Checkboxen namens
# "doclist_item_<ID>_<DOKID>". Nach dem Absenden des Formulars (GET auf
# eine relative URL, meist "location") landet man je nach Anliegen direkt
# auf der Kalenderseite oder zunächst auf einer Standort-Auswahl.
#
# Um die Fehleranfälligkeit bei über 20 unterschiedlichen Anliegen gering
# zu halten, wird JEDES Anliegen EINZELN geprüft (eigene frische Sitzung),
# nicht alle gleichzeitig.

from urllib.parse import urljoin  # noqa: E402


def discover_tevis_concerns(soup: BeautifulSoup) -> list[dict]:
    concerns = []
    number_inputs = soup.find_all("input", {"type": "number", "name": re.compile(r"^cnc-\d+$")})
    for inp in number_inputs:
        concern_id = inp["name"].split("-", 1)[1]

        label = None
        plus_btn = soup.find("button", {"data-field": f"cnc-{concern_id}", "data-type": "plus"})
        if plus_btn and plus_btn.get("aria-label"):
            m = re.search(r"Anliegens (.+)$", plus_btn["aria-label"])
            if m:
                label = m.group(1).strip()
        if not label:
            label = f"Anliegen {concern_id}"

        # Die Dokument-Bestätigungs-Checkboxen stecken NICHT im sichtbaren
        # Formular, sondern als HTML-Text im Attribut "data-tevis-cncpaper"
        # des Mengenfelds selbst (wird von TEVIS erst per JavaScript in die
        # Seite eingefügt, sobald die Menge > 0 gesetzt wird).
        doc_fields: list[tuple[str, str]] = []
        cncpaper = inp.get("data-tevis-cncpaper", "")
        if cncpaper:
            paper_soup = BeautifulSoup(cncpaper, "html.parser")
            for cb in paper_soup.find_all("input", {"type": "checkbox"}):
                if cb.get("name"):
                    doc_fields.append((cb["name"], cb.get("value", "on")))

        concerns.append({"id": concern_id, "label": label, "doc_fields": doc_fields})
    return concerns


TEVIS_NEXT_DATE_PATTERN = re.compile(r"Nächster Termin ab (\d{1,2}\.\d{1,2}\.\d{4}), (\d{1,2}:\d{2}) Uhr")


def find_tevis_next_dates(html: str) -> list[str]:
    """TEVIS zeigt auf der Schritt-3-Seite (Terminvorschläge/Standortauswahl)
    pro möglichem Standort direkt den nächsten freien Termin als Text an,
    z. B. 'Nächster Termin ab 21.09.2026, 10:05 Uhr'. Das ist zuverlässiger
    als die generische Kalender-Heuristik."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    matches = TEVIS_NEXT_DATE_PATTERN.findall(text)
    return sorted({f"{d}, {t} Uhr" for d, t in matches})


def check_tevis(key: str, select2_url: str) -> dict:
    """Prüft eine TEVIS-Terminseite, Anliegen für Anliegen, und liefert für
    jedes Anliegen eine eigene Verfügbarkeits-Liste zurück."""

    probe_session = requests.Session()
    resp = probe_session.get(select2_url, headers=REQUEST_HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    save_debug(key, resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", {"id": "cnc-select-form"}) or soup.find("form")
    if form is None:
        raise RuntimeError("Kein Anliegen-Formular gefunden (Schritt 2 von 6).")

    base_hidden: dict[str, str] = {}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        if name:
            base_hidden[name] = hidden.get("value", "")

    concerns = discover_tevis_concerns(soup)
    if not concerns:
        raise RuntimeError(
            "Konnte die Anliegen-Mengenfelder (cnc-<ID>) nicht finden. Die "
            "Struktur der Seite hat sich vermutlich geändert."
        )

    action = form.get("action") or "location"
    action_url = urljoin(select2_url, action)

    per_concern: dict[str, dict] = {}
    all_ids = [c["id"] for c in concerns]
    first_error_debug_saved = False

    for concern in concerns:
        payload = dict(base_hidden)
        for other_id in all_ids:
            payload[f"cnc-{other_id}"] = "0"
        payload[f"cnc-{concern['id']}"] = "1"
        for doc_name, doc_value in concern["doc_fields"]:
            payload[doc_name] = doc_value

        try:
            session = requests.Session()
            # Cookies/Session der ursprünglichen Seite übernehmen, damit
            # eine eventuelle Server-Sitzung erhalten bleibt.
            session.cookies.update(probe_session.cookies)

            step_resp = session.get(action_url, params=payload, headers=REQUEST_HEADERS, timeout=TIMEOUT, allow_redirects=True)
            step_resp.raise_for_status()

            if not first_error_debug_saved:
                save_debug(key, step_resp.text)
                first_error_debug_saved = True

            tevis_dates = find_tevis_next_dates(step_resp.text)
            page_text_lower = BeautifulSoup(step_resp.text, "html.parser").get_text(" ", strip=True).lower()
            advanced_to_step3 = "schritt 3 von 6" in page_text_lower

            if tevis_dates:
                per_concern[concern["label"]] = {"status": "ok", "available": tevis_dates}
            elif advanced_to_step3:
                # Auswahl hat funktioniert (Schritt 3 erreicht), aber kein
                # "Nächster Termin ab..."-Text gefunden -> vermutlich
                # tatsächlich nichts frei.
                per_concern[concern["label"]] = {"status": "ok", "available": []}
            elif explicit_no_appointments(step_resp.text):
                per_concern[concern["label"]] = {"status": "ok", "available": []}
            else:
                per_concern[concern["label"]] = {
                    "status": "error",
                    "available": [],
                    "message": "Anliegen-Auswahl hat nicht wie erwartet zu Schritt 3 (Terminvorschläge) geführt.",
                }

        except Exception as exc:  # noqa: BLE001
            per_concern[concern["label"]] = {"status": "error", "available": [], "message": str(exc)}

    return {"concerns": per_concern}


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
                if isinstance(data, dict) and "sites" in data:
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
        prev_site = previous_sites.get(key, {"status": "unknown", "concerns": {}})
        prev_concerns = prev_site.get("concerns", {})

        try:
            result = site["check"]()

            if "concerns" in result:
                concerns = result["concerns"]
            else:
                concerns = {"Termine": {"status": "ok", "available": result.get("available", [])}}

            all_new_dates: list[str] = []
            any_error = False
            for concern_label, concern_data in concerns.items():
                prev_concern = prev_concerns.get(concern_label, {"status": "unknown", "available": []})
                available = concern_data.get("available", [])
                if concern_data.get("status") == "error":
                    any_error = True
                    continue
                prev_available = set(prev_concern.get("available", []))
                new_dates = sorted(set(available) - prev_available)
                if new_dates:
                    all_new_dates.append(f"{concern_label}: " + ", ".join(new_dates))

            if all_new_dates:
                notify(
                    f"Neuer freier Termin in {label}!",
                    "\n".join(all_new_dates) + f"\nJetzt buchen: {url}",
                    priority="urgent",
                    tags="tada,calendar",
                )

            new_sites[key] = {
                "label": label,
                "url": url,
                "status": "error" if any_error and all(c.get("status") == "error" for c in concerns.values()) else "ok",
                "concerns": concerns,
            }
            ok_count = sum(1 for c in concerns.values() if c.get("status") == "ok")
            print(f"[{key}] {ok_count}/{len(concerns)} Anliegen erfolgreich geprüft.")

        except Exception as exc:  # noqa: BLE001
            error_text = f"{exc}"
            print(f"[{key}] Fehler beim Prüfen:", error_text, file=sys.stderr)
            traceback.print_exc()

            new_sites[key] = {
                "label": label,
                "url": url,
                "status": "error",
                "concerns": prev_concerns,
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
    print("status.json geschrieben.")


if __name__ == "__main__":
    main()
