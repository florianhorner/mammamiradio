# Codebase-Review: konkrete Aufgabenvorschläge (2026-04-28)

## 1) Aufgabe: Tippfehler in Quell-ID der Charts korrigieren

**Problem**
- Die Charts-Quelle verwendet die Kennung `apple_music_it_top_50`, während URL und Fetch-Logik auf **100** Titel ausgelegt sind (`most-played/100`, `limit=100`).
- Das wirkt wie ein Copy/Paste- bzw. Nummern-Tippfehler in der Identifier-Konstante.

**Fundstellen**
- `mammamiradio/playlist.py` (`source_id="apple_music_it_top_50"` in `_charts_source`).
- `mammamiradio/playlist.py` (`_APPLE_MUSIC_IT_CHARTS_URL` mit `.../most-played/100/songs.json`).
- `mammamiradio/playlist.py` (`_fetch_current_italy_charts(limit: int = 100, ...)`).

**Vorschlag für Ticket**
- `source_id` auf `apple_music_it_top_100` umstellen.
- Optional: Migrations-/Kompatibilitätslogik ergänzen, falls persistierte Quellen (`playlist_source.json`) noch `..._top_50` enthalten.

---

## 2) Aufgabe: Programmierfehler – lokale Musik wird bei deaktiviertem yt-dlp nicht als Quelle genutzt

**Problem**
- In `fetch_startup_playlist()` werden lokale MP3s nur über den Charts-Pfad eingeblendet.
- Wenn `allow_ytdlp=False`, wird der Charts-Pfad übersprungen; lokale Tracks werden dann **nicht** geladen, obwohl sie vorhanden sind.
- In diesem Fall landet der Start bei Demo/Jamendo statt bei lokalem Material.

**Fundstellen**
- `mammamiradio/playlist.py` (`charts_allowed = config.allow_ytdlp` + Charts-Pfad nur bei `True`).
- `mammamiradio/playlist.py` (`local_present` prüft nur auf Warnung, lädt aber keine lokale Playlist als Fallback).

**Vorschlag für Ticket**
- Eigenen Fallback-Pfad implementieren: Wenn Charts aus sind oder fehlschlagen und lokale MP3s vorhanden sind, lokale Playlist direkt als Startup-Quelle nutzen (`source.kind="local"` oder konsistente Kennung).
- Nebenbei die Warning-Message aktualisieren, damit Verhalten und Text übereinstimmen.

---

## 3) Aufgabe: Kommentar-/Doku-Unstimmigkeit bereinigen

**Problem**
- README behauptet bei fehlendem `jamendo_client_id` einen Fallback „zu lokalen Dateien oder Charts“.
- Die aktuelle Laufzeitlogik macht bei deaktiviertem yt-dlp jedoch keinen direkten lokalen Fallback in `fetch_startup_playlist()`.
- Damit beschreibt die Doku ein Verhalten, das so nicht zuverlässig eintritt.

**Fundstellen**
- `README.md` (Tabelle „Never Crashes, Always Plays“, Zeile `jamendo_client_id`-Fallback).
- `mammamiradio/playlist.py` (`fetch_startup_playlist()`-Ablauf).

**Vorschlag für Ticket**
- README-Fallbackmatrix mit tatsächlicher Logik synchronisieren **oder** Logik wie dokumentiert implementieren (bevorzugt: Logik fixen, dann Doku bestätigen).
- Ergänzend in `docs/architecture.md` den Startup-Fallback-Pfad explizit ausformulieren.

---

## 4) Aufgabe: Testverbesserung – Regressionstest für Local-Fallback ohne yt-dlp

**Problem**
- Es gibt Tests für „Local-Merge in Charts“ und eine Warnung bei deaktiviertem yt-dlp.
- Es fehlt aber ein klarer Testfall für: `allow_ytdlp=False`, Jamendo nicht verfügbar, lokale MP3s vorhanden ⇒ erwartetes Startup-Verhalten (derzeit Demo; nach Fix lokal).

**Fundstellen**
- `tests/test_playlist_fetch.py` (Local-Merge nur im Charts-Kontext).
- `tests/test_jamendo_coverage.py` (Warnungs-Tests, aber kein End-to-End-Fallbackziel auf lokal).

**Vorschlag für Ticket**
- Neuen Test in `tests/test_playlist_fetch.py` ergänzen:
  - Setup: lokale MP3 vorhanden, `allow_ytdlp=False`, Jamendo disabled/leer.
  - Assert: Quelle und Trackliste entsprechen der gewünschten Local-Fallback-Policy.
- Optional zusätzlich Persistenzfall testen (`read_persisted_source` + Local-Fallback).
