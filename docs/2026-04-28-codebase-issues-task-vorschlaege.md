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
- Migrations-/Kompatibilitätslogik in `read_persisted_source()` ergänzen: Beim Laden der `playlist_source.json` jeden Eintrag mit `source_id == "apple_music_it_top_50"` automatisch auf `"apple_music_it_top_100"` remappen (kein Fehler, transparente Migration). **Beide IDs sollen nicht als separate Legacy-Einträge koexistieren.** Akzeptanzkriterien:
  - Ein persistiertes `playlist_source.json` mit `"apple_music_it_top_50"` wird beim nächsten Start ohne Warnung als `"apple_music_it_top_100"` geladen.
  - Einheit-Test: `read_persisted_source()` mit `source_id = "apple_music_it_top_50"` gibt `source_id == "apple_music_it_top_100"` zurück.

---

## 2) Aufgabe: Programmierfehler – lokale Musik wird bei deaktiviertem yt-dlp nicht als Quelle genutzt

**Problem**
- In `fetch_startup_playlist()` werden lokale MP3s nur über den Charts-Pfad eingeblendet.
- Wenn `allow_ytdlp=False`, wird der Charts-Pfad übersprungen; lokale Tracks werden dann **nicht** geladen, obwohl sie vorhanden sind.
- In diesem Fall landet der Start bei Demo/Jamendo statt bei lokalem Material.

**Fundstellen**
- `mammamiradio/playlist.py` (`charts_allowed = config.allow_ytdlp` + Charts-Pfad nur bei `True`).
- `mammamiradio/playlist.py` (`local_present` prüft nur auf Warnung, lädt aber keine lokale Playlist als Fallback).

**Semantische Entscheidung (Akzeptanzkriterium)**
`allow_ytdlp=False` bedeutet **„kein yt-dlp-Download"**, nicht „keine lokalen Dateien". Lokale MP3s in `music/` sind bereits vorhanden und brauchen kein yt-dlp. Daher gilt: **Option 1 — lokale Dateien als Startup-Fallback sind auch bei `allow_ytdlp=False` erlaubt.**

**Vorschlag für Ticket**
- Eigenen Fallback-Pfad in `fetch_startup_playlist()` einbauen: Direkt nach dem Jamendo-Block, wenn `local_present` gilt, lokale Playlist laden und zurückgeben (`source.kind="local"`). Demo-Assets werden nur noch genutzt, wenn auch lokal nichts vorhanden ist.
- Die bestehende Warning-Message von „set it to 'true' to blend local tracks" auf „set it to 'true' to also enable live chart blending" korrigieren — lokale Dateien funktionieren ohne das Flag.
- Akzeptanzkriterien:
  - `allow_ytdlp=False` + `music/*.mp3` vorhanden ⇒ Startup nutzt lokale Tracks (kein Demo).
  - `allow_ytdlp=False` + kein `music/*.mp3` ⇒ Startup nutzt Demo (unverändertes Verhalten).
  - `allow_ytdlp=True` + `music/*.mp3` vorhanden ⇒ Charts werden genutzt (unverändertes Verhalten).

---

## 3) Aufgabe: Kommentar-/Doku-Unstimmigkeit bereinigen

**Problem**
- README behauptet bei fehlendem `jamendo_client_id` einen Fallback „zu lokalen Dateien oder Charts“.
- Die aktuelle Laufzeitlogik macht bei deaktiviertem yt-dlp jedoch keinen direkten lokalen Fallback in `fetch_startup_playlist()`.
- Damit beschreibt die Doku ein Verhalten, das so nicht zuverlässig eintritt.

**Fundstellen**
- `README.md` (Tabelle „Never Crashes, Always Plays“, Zeile `jamendo_client_id`-Fallback).
- `mammamiradio/playlist.py` (`fetch_startup_playlist()`-Ablauf).

**Tatsächliche Prioritätsreihenfolge von `fetch_startup_playlist()` (Stand 2026-04-28)**

```
1. Persistierte Quelle (playlist_source.json) — falls vorhanden und ladbar
2. Charts via yt-dlp — nur wenn allow_ytdlp=True
3. Jamendo — nur wenn jamendo_client_id konfiguriert
4. ← LÜCKE: lokale MP3s (music/*.mp3) werden nur gewarnt, nicht geladen
5. Bundled Demo-Assets (assets/demo/music/)
6. DEMO_TRACKS-Konstante (Built-in-Fallback)
```

**Vorschlag für Ticket**
- README-Fallbackmatrix mit tatsächlicher Logik synchronisieren **oder** Logik wie dokumentiert implementieren (bevorzugt: nach Task 2-Fix Logik bestätigen, dann Doku aktualisieren).
- In `docs/architecture.md` die obige Prioritätsreihenfolge explizit tabellarisch ausformulieren, inklusive der Bedingungen (`allow_ytdlp`, `jamendo_client_id`).
- Die README-Zeile für `jamendo_client_id` in der „Never Crashes, Always Plays"-Matrix korrigieren: aktueller Text suggeriert Fallback auf lokale Dateien, der tatsächlich nicht stattfindet (bis Task 2 umgesetzt ist).

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
