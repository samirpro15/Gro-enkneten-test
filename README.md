# Terminradar Großenkneten

Prüft automatisch alle 15 Minuten die offizielle Online-Terminvergabe der
Gemeinde Großenkneten (crossing.de) auf freie Behördentermine, zeigt den
Status in einer kleinen installierbaren Web-App und schickt eine
Push-Benachrichtigung, sobald ein neuer freier Termin auftaucht.

**Bucht nichts automatisch.** Gebucht wird weiterhin auf der offiziellen
Seite: https://termine.crossing.de/4878781/Appointment/Index/1

Alles läuft kostenlos auf GitHub (Actions + Pages) und über den kostenlosen
Push-Dienst [ntfy.sh](https://ntfy.sh) – du brauchst keinen eigenen Server.

---

## Einmalige Einrichtung (ca. 10 Minuten)

### 1. GitHub-Repository anlegen
1. Falls noch nicht vorhanden: kostenloses Konto auf [github.com](https://github.com) anlegen.
2. Oben rechts auf **+** → **New repository**.
3. Name z. B. `terminradar-grossenkneten`, Sichtbarkeit **Public** (bei
   Private funktioniert GitHub Pages nur mit einem bezahlten Plan).
4. **Create repository**.

### 2. Dateien hochladen
1. Im neuen, leeren Repository auf **uploading an existing file** klicken
   (oder Add file → Upload files).
2. **Alle** Dateien und Ordner aus diesem Projekt hochladen – wichtig ist,
   dass der Ordner `.github/workflows/` mit der Datei `check-termine.yml`
   **genau in diesem Pfad** landet. Am einfachsten: den kompletten
   Projektordner per Drag & Drop in das Upload-Feld ziehen, GitHub behält
   die Ordnerstruktur bei.
3. Commit-Nachricht eingeben (z. B. „Erste Version“) → **Commit changes**.

### 3. Push-Thema (ntfy) einrichten
1. Kostenlose App **ntfy** installieren:
   [iOS](https://apps.apple.com/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. App öffnen → **+** (Thema abonnieren) → als Thema eingeben:
   ```
   grossenkneten-termine-35a2a9408f
   ```
   (Server bleibt `ntfy.sh`, das ist die Voreinstellung.)
3. Du kannst statt diesem auch ein eigenes, schwer erratbares Thema
   wählen – dann aber in **zwei** Dateien anpassen: hier beim Abonnieren
   und unten in Schritt 5 als Secret-Wert.

> Ein ntfy-Thema ist wie ein Radiosender: Wer den Namen kennt, kann
> mithören. Der zufällige Name oben ist bewusst schwer zu erraten, aber
> nicht geheim im kryptografischen Sinn. Für persönliche Termin-Updates
> reicht das üblicherweise aus.

### 4. GitHub Actions Schreibrechte geben
Die automatische Prüfung muss den aktuellen Status ins Repository
zurückschreiben können:
1. Im Repository: **Settings** → **Actions** → **General**.
2. Ganz unten bei **Workflow permissions**: **Read and write permissions**
   auswählen → **Save**.

### 5. Secret für den Push-Dienst hinterlegen
1. **Settings** → **Secrets and variables** → **Actions** → **New
   repository secret**.
2. Name: `NTFY_TOPIC`
3. Wert: `grossenkneten-termine-35a2a9408f` (oder dein eigenes Thema aus
   Schritt 3).
4. **Add secret**.

### 6. GitHub Pages aktivieren
1. **Settings** → **Pages**.
2. Bei **Build and deployment** → **Branch**: `main` und Ordner `/(root)`
   auswählen → **Save**.
3. Nach 1–2 Minuten ist die App erreichbar unter:
   `https://<dein-github-name>.github.io/terminradar-grossenkneten/`

### 7. Ersten Check manuell auslösen
1. Reiter **Actions** → links **Terminradar Großenkneten prüfen**.
2. **Run workflow** → **Run workflow**.
3. Nach ca. 30–60 Sekunden ist der Lauf fertig; auf der GitHub-Pages-Seite
   (Schritt 6) sollte jetzt ein Status stehen statt „Wartet auf ersten
   Check“.

### 8. Als App auf dem Handy installieren
Die URL aus Schritt 6 auf dem Handy öffnen:
- **iPhone (Safari):** Teilen-Symbol → „Zum Home-Bildschirm“.
- **Android (Chrome):** Menü (⋮) → „App installieren“.

Fertig – ab jetzt läuft die Prüfung automatisch alle 15 Minuten im
Hintergrund, auch wenn dein Handy die App nicht geöffnet hat. Bei einem
neuen freien Termin kommt eine Push-Benachrichtigung über ntfy.

---

## Falls der erste Check „Technischer Fehler“ meldet

Die Terminseite von Großenkneten hat keine öffentliche Schnittstelle –
das Skript liest die normale Webseite aus. Die Auswahl der Anliegen
funktioniert bei manchen Systemen dieser Art per JavaScript statt über
ein normales Formular; das lässt sich vorab nicht zu 100 % prüfen.

Falls das passiert (du bekommst dazu eine Push-Benachrichtigung, und die
App zeigt „Technischer Fehler“ statt eines Status):

1. Im fehlgeschlagenen Actions-Lauf ganz unten bei **Artifacts** die Datei
   **debug-html** herunterladen.
2. Mir (Claude) die Datei schicken bzw. den Inhalt beschreiben.
3. Ich passe dann gezielt die Funktion `find_available_dates(...)` bzw.
   `load_concern_form(...)` in `check_termine.py` an – meist reicht eine
   kleine Änderung.

## Dateien in diesem Projekt

| Datei | Zweck |
|---|---|
| `check_termine.py` | Prüft die Terminseite, vergleicht mit dem letzten Stand, schickt Push |
| `.github/workflows/check-termine.yml` | Lässt das Skript alle 15 Minuten automatisch laufen |
| `status.json` | Letzter bekannter Status (wird automatisch aktualisiert) |
| `index.html`, `manifest.json`, `sw.js`, `icons/` | Die installierbare Status-App |
| `README.md` | Diese Anleitung |

## Später: Wildeshausen und Stadt Oldenburg ergänzen

Sobald du mir die Links zu deren Online-Terminvergabe schickst, baue ich
dieselbe Logik für die beiden Orte dazu – entweder als zusätzliche
Kacheln in derselben App oder als eigene Instanz, wie du magst.
