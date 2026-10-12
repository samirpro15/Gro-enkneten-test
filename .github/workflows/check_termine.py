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
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import urljoin

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

#
# Die Anliegen-Auswahl auf crossing.de läuft NICHT über das normale
# Formular: Ein Klick auf "+" schickt per JavaScript (Scripts/
# _SelectConcernFromGroup.js) einen AJAX-POST an URL_CS mit
# {concernID, count}. Erst dadurch merkt sich der Server die Auswahl.
# Schickt man nur das Formular ab, leitet die Seite auf
# "SelectConcern?noConcernsSelected=True" um – die alte Version hat das
# nicht bemerkt und deshalb immer "keine freien Termine" gemeldet.

CROSSING_HOST = "https://termine.crossing.de"
CROSSING_ORG = "4878781"
URL_CS_PATTERN = re.compile(r"URL_CS\s*=\s*['\"]([^'\"]+)['\"]")


def crossing_token(form) -> str:
    token = form.find("input", {"name": "__RequestVerificationToken"})
    return token.get("value", "") if token else ""


def check_grossenkneten() -> dict:
    index_url = f"{CROSSING_HOST}/{CROSSING_ORG}/Appointment/Index/1"
    calendar_url = f"{CROSSING_HOST}/{CROSSING_ORG}/Appointment/SelectDateAndTime"

    probe = requests.get(index_url, headers=REQUEST_HEADERS, timeout=TIMEOUT)
    probe.raise_for_status()
    save_debug("grossenkneten", probe.text)

    soup = BeautifulSoup(probe.text, "html.parser")
    concerns = []
    for row in soup.select(".concern-row[data-concern-id]"):
        label_el = row.find("span")
        label = label_el.get_text(" ", strip=True).rstrip("→ ").strip() if label_el else ""
        concerns.append({"id": row["data-concern-id"], "label": label or f"Anliegen {row['data-concern-id']}"})
    if not concerns:
        raise RuntimeError("Keine Anliegen (.concern-row) gefunden. Die Struktur der Seite hat sich vermutlich geändert.")

    match = URL_CS_PATTERN.search(probe.text)
    if not match:
        raise RuntimeError("AJAX-Adresse für die Anliegen-Auswahl (URL_CS) nicht gefunden.")
    select_url = urljoin(index_url, match.group(1))

    per_concern: dict[str, dict] = {}
    for concern in concerns:
        try:
            session = requests.Session()
            resp = session.get(index_url, headers=REQUEST_HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            form = BeautifulSoup(resp.text, "html.parser").find("form", {"id": "form-add-concern-items"})
            if form is None:
                raise RuntimeError("Anliegen-Formular nicht gefunden.")
            token = crossing_token(form)

            # 1) Auswahl serverseitig setzen – genau wie der "+"-Knopf.
            sel = session.post(
                select_url,
                data={"concernID": concern["id"], "count": "1", "__RequestVerificationToken": token},
                headers={**REQUEST_HEADERS, "X-Requested-With": "XMLHttpRequest"},
                timeout=TIMEOUT,
            )
            sel.raise_for_status()

            # 2) Formular abschicken.
            payload = {"__RequestVerificationToken": token}
            for other in concerns:
                payload[f"concern-{other['id']}"] = "1" if other["id"] == concern["id"] else "0"
            session.post(urljoin(index_url, form.get("action") or index_url), data=payload,
                         headers=REQUEST_HEADERS, timeout=TIMEOUT).raise_for_status()

            # 3) Kalender laden.
            cal = session.get(calendar_url, headers=REQUEST_HEADERS, timeout=TIMEOUT, allow_redirects=True)
            cal.raise_for_status()

            if "noConcernsSelected" in cal.url or "SelectDateAndTime" not in cal.url:
                save_debug("grossenkneten", cal.text)
                per_concern[concern["label"]] = {
                    "status": "error",
                    "available": [],
                    "message": "Anliegen-Auswahl wurde vom Server nicht übernommen (kein Kalender erreicht).",
                }
            elif explicit_no_appointments(cal.text):
                per_concern[concern["label"]] = {"status": "ok", "available": []}
            else:
                per_concern[concern["label"]] = {"status": "ok", "available": find_available_dates(cal.text)}
        except Exception as exc:  # noqa: BLE001
            per_concern[concern["label"]] = {"status": "error", "available": [], "message": str(exc)}
        time.sleep(0.5)  # die Gemeinde-Seite nicht hämmern

    return {"concerns": per_concern}


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


SLOT_PATTERN = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:,?\s*(\d{1,2}):(\d{2}))?")


def earliest_slot(values: list[str]) -> datetime | None:
    """Frühester Termin aus Texten wie '13.10.2026, 11:00 Uhr' oder '13.10.26'."""
    slots = []
    for value in values:
        m = SLOT_PATTERN.search(value)
        if not m:
            continue
        day, month, year, hour, minute = m.groups()
        year = int(year) + 2000 if len(year) == 2 else int(year)
        try:
            slots.append(datetime(year, int(month), int(day), int(hour or 0), int(minute or 0)))
        except ValueError:
            continue
    return min(slots) if slots else None


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
                # Nur melden, wenn es einen FRÜHEREN Termin gibt als beim
                # letzten Lauf (oder vorher gar keinen). Ein bloßes
                # Verschieben des "Nächster Termin ab ..." nach hinten, weil
                # der alte Termin gebucht wurde, ist keine Neuigkeit.
                first = earliest_slot(available)
                prev_first = earliest_slot(prev_concern.get("available", []))
                if first and (prev_first is None or first < prev_first):
                    all_new_dates.append(f"{concern_label}: ab {first.strftime('%d.%m.%Y, %H:%M')} Uhr")

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
