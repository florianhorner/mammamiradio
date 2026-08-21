"""Listener UI copy lookup driven by `super_italian_mode`.

Decorative Italian (station-feel headlines, brand idioms, section names) lives
in the templates verbatim and stays Italian regardless of mode. This module
holds only the **swappable** strings — buttons, form labels/placeholders, and
JS-side dynamic labels — that flip between English (default, OFF) and Italian
(Super Italian Mode, ON).

Admin UI is intentionally not routed through this module; it always renders
in English.
"""

from __future__ import annotations

COPY: dict[str, dict[str, str]] = {
    "en": {
        # Listener page — buttons, CTAs, aria labels
        "listen_now": "Listen Now",
        "listen_pause": "Pause",
        "listen_stopped": "Station paused",
        "listen_now_aria": "Listen now",
        "listen_pause_aria": "Pause station",
        "listen_paused_aria": "Station paused",
        "share_clip": "Share clip",
        "share_clip_aria": "Share the current clip",
        "schedule_button": "Schedule",
        "install_app": "Install app",
        "footer_listen": "Listen",
        "music_credits": "Music credits",
        "credits_dialog_title": "Music credits and sources",
        "current_track_credit": "Current track",
        "included_music_catalog": "Included starter collection",
        "credits_close": "Close credits",
        "credits_source": "Source",
        "credits_license": "License",
        "credits_licensed_under": "Licensed under {license}",
        "credits_provided_by_jamendo": "Provided by Jamendo under {license}",
        "credits_no_current_music": "No music is playing right now.",
        "credits_catalog_unavailable": "The included collection is not available in this build.",
        "local_credit": "Provided by the station operator; rights remain their responsibility.",
        "source_unavailable": "Source details are unavailable for this track.",
        "normalized_notice": "Normalized and transcoded by Mamma Mi Radio; no musical edits.",
        "provider_reported_notice": "License and source details are reported by the provider, not a clearance verdict.",
        # Listener page — stat labels under hero
        "stat_airtime": "On Air Today",
        "casa_moments_title": "On-air moments from your home",
        "casa_moments_helper": "This is a record of home moments that made it on air—not every change at home.",
        "casa_moment_airing": "on air now",
        "casa_moment_minutes_ago": "{m} min ago",
        "casa_moment_hours_ago": "{h} hr ago",
        "casa_moment_yesterday": "yesterday",
        "casa_moment_days_ago": "{d} days ago",
        "casa_moment_stale": "Nothing newer has made it on air yet.",
        "stat_tracks": "Tracks in Rotation",
        "stat_hosts": "Hosts",
        # Listener page — placeholders / loading copy
        "tuning_in": "Tuning in…",
        "schedule_loading": "Schedule loading…",
        "waiting_dedication": "Waiting for the first dedication tonight…",
        # Dediche form
        "form_name_label": "Name (optional)",
        "form_name_placeholder": "Your name (optional)",
        "form_message_label": "Message or song request",
        "form_message_placeholder": "Dear Radio, I'd like to dedicate a song to…",
        "form_message_required": "Write a message first, then send it to the DJ.",
        "form_submit": "Send with a kiss",
        "form_success_song": "Request received. We’re checking the catalogue for a matching recording…",
        "form_success_shoutout": "Dedication received! The hosts will read it soon.",
        "form_song_searching": "Request received. We’re checking the catalogue for a matching recording…",
        "form_song_matched": "We found {track}. It’s ready for the hosts to introduce.",
        "form_song_matched_generic": "We found a match. It’s ready for the hosts to introduce.",
        "form_song_no_verified_match": (
            "We couldn’t find a clear match for that request. Try again with the exact song title and artist."
        ),
        "form_song_not_playable": (
            "We found a possible match, but couldn’t prepare it for air. Try another title or artist."
        ),
        "form_song_temporarily_unavailable": (
            "We couldn’t finish that song request this time. Your message is still here — "
            "try again later or rewrite it as a dedication instead."
        ),
        "form_song_tracking_expired": (
            "We can’t track that request any longer. Your message is still here — send it again to restart the search."
        ),
        "form_rate_limited": "Give the DJ {s}s before sending another dedication.",
        "form_queue_full": "The dedication queue is full — wait a moment and try again.",
        "form_declined": "That dedication didn't go through — wait a moment and try again.",
        "form_network_error": "We lost the connection — check it and try again.",
        # listener.js dynamic labels (served via /public-status payload)
        "now": "now",
        "minutes_ago": "min ago",
        "hours_ago": "hr ago",
        "seg_music": "Music",
        "seg_banter": "Banter",
        "seg_ad": "Sponsored",
        "seg_news": "News",
        "seg_jingle": "Jingle",
        "seg_welcome": "Welcome",
        "seg_idle": "Idle",
        "seg_default": "On Air",
        # Now-playing strip + palinsesto inline strings rendered by listener.js
        "np_paused": "Paused",
        "np_stopped": "Stopped",
        "skip_to_content": "Skip to content",
        "np_welcome": "Welcome aboard",
        "np_ad_message": "Sponsored message",
        "np_ad_break": "This ad break",
        "ad_session_summary_one": "This session · 1 completed spot",
        "ad_session_summary": "This session · {n} completed spots",
        "ad_session_airings_one": "1 completed airing",
        "ad_session_airings": "{n} completed airings",
        "np_banter_strip": "in conversation",
        "np_banter_idle": "The hosts are on air",
        "np_on_air": "On Air",
        "np_now": "On now",
        "np_next": "Next",
        "np_building": "The next records are being cued…",
        "np_no_source": "No records are loaded yet — check back once the crate is filled.",
        "np_live": "Live",
        # Clip sharing — warm, in-character, and every error names the way out
        # (leadership principle #5). {s} is filled with the retry seconds by JS.
        "clip_saving": "Saving your clip…",
        "clip_copied": "Link copied — paste it anywhere to share.",
        "clip_rate_limited": "The tape decks are still spooling your last clip — give them {s}s and tap again.",
        "clip_no_audio": "Nothing to clip just yet — let the radio play for a moment, then tap Share.",
        "music_share_unavailable": "A complete included track has to finish before it can be shared.",
        "clip_error": "That clip didn't take — give it a moment and tap Share again.",
        "clip_copy_prompt": "Copy this link:",
    },
    "it": {
        "listen_now": "Ascolta Ora",
        "listen_pause": "Pausa",
        "listen_stopped": "Radio in pausa",
        "listen_now_aria": "Ascolta ora",
        "listen_pause_aria": "Metti in pausa la radio",
        "listen_paused_aria": "Radio in pausa",
        "share_clip": "Condividi clip",
        "share_clip_aria": "Condividi la clip corrente",
        "schedule_button": "Il Palinsesto",
        "install_app": "Installa app",
        "footer_listen": "Ascolta",
        "music_credits": "Crediti musicali",
        "credits_dialog_title": "Crediti e fonti musicali",
        "current_track_credit": "Brano in onda",
        "included_music_catalog": "Collezione iniziale inclusa",
        "credits_close": "Chiudi i crediti",
        "credits_source": "Fonte",
        "credits_license": "Licenza",
        "credits_licensed_under": "Concesso in licenza con {license}",
        "credits_provided_by_jamendo": "Fornito da Jamendo con licenza {license}",
        "credits_no_current_music": "Al momento non c'è musica in onda.",
        "credits_catalog_unavailable": "La collezione inclusa non è disponibile in questa build.",
        "local_credit": "Fornito dall'operatore della radio; i diritti restano sotto la sua responsabilità.",
        "source_unavailable": "I dettagli della fonte non sono disponibili per questo brano.",
        "normalized_notice": "Normalizzato e transcodificato da Mamma Mi Radio; nessuna modifica musicale.",
        "provider_reported_notice": (
            "Licenza e fonte sono dichiarate dal provider, non costituiscono una verifica dei diritti."
        ),
        "stat_airtime": "In onda oggi",
        "casa_moments_title": "Momenti dalla tua casa andati in onda",
        "casa_moments_helper": (
            "Questo è il registro dei momenti di casa andati in onda, non di ogni cambiamento a casa."
        ),
        "casa_moment_airing": "in onda ora",
        "casa_moment_minutes_ago": "{m} min fa",
        "casa_moment_hours_ago": "{h} h fa",
        "casa_moment_yesterday": "ieri",
        "casa_moment_days_ago": "{d} giorni fa",
        "casa_moment_stale": "Non ci sono ancora momenti più recenti andati in onda.",
        "stat_tracks": "Tracce in playlist",
        "stat_hosts": "I conduttori",
        "tuning_in": "Stiamo accendendo la radio…",
        "schedule_loading": "Il palinsesto sta arrivando…",
        "waiting_dedication": "Aspettiamo la prima dedica della sera…",
        "form_name_label": "Nome (opzionale)",
        "form_name_placeholder": "Come ti chiami? (opzionale)",
        "form_message_label": "Messaggio o richiesta musicale",
        "form_message_placeholder": "Cara Radio, vorrei dedicare una canzone a…",
        "form_message_required": "Scrivi prima un messaggio, poi spediscilo al DJ.",
        "form_submit": "Spedisci con un bacio",
        "form_success_song": "Richiesta ricevuta. Cerchiamo in catalogo una registrazione corrispondente…",
        "form_success_shoutout": "Dedica ricevuta! I conduttori la leggeranno presto.",
        "form_song_searching": "Richiesta ricevuta. Cerchiamo in catalogo una registrazione corrispondente…",
        "form_song_matched": "Abbiamo trovato {track}. I conduttori ora possono presentarla.",
        "form_song_matched_generic": "Abbiamo trovato una corrispondenza. I conduttori ora possono presentarla.",
        "form_song_no_verified_match": (
            "Non abbiamo trovato una corrispondenza chiara per questa richiesta. "
            "Riprova indicando titolo esatto e artista."
        ),
        "form_song_not_playable": (
            "Abbiamo trovato una possibile corrispondenza, ma non siamo riusciti a prepararla "
            "per la messa in onda. Prova un altro titolo o artista."
        ),
        "form_song_temporarily_unavailable": (
            "Questa volta non siamo riusciti a completare la richiesta musicale. Il messaggio è ancora qui — "
            "riprova più tardi oppure riscrivilo come dedica."
        ),
        "form_song_tracking_expired": (
            "Non possiamo più seguire questa richiesta. Il messaggio è ancora qui — "
            "invialo di nuovo per ricominciare la ricerca."
        ),
        "form_rate_limited": "Aspetta {s}s prima di mandare un'altra dedica.",
        "form_queue_full": "La coda delle dediche è piena — aspetta un attimo e riprova.",
        "form_declined": "La dedica non è partita — aspetta un attimo e riprova.",
        "form_network_error": "Abbiamo perso la connessione — controllala e riprova.",
        "now": "adesso",
        "minutes_ago": "min fa",
        "hours_ago": "h fa",
        "seg_music": "Musica",
        "seg_banter": "Banter",
        "seg_ad": "Sponsorizzato",
        "seg_news": "Notizie",
        "seg_jingle": "Jingle",
        "seg_welcome": "Benvenuto",
        "seg_idle": "In attesa",
        "seg_default": "In onda",
        "np_paused": "In pausa",
        "np_stopped": "Fermo",
        "skip_to_content": "Salta al contenuto",
        "np_welcome": "Ben arrivato",
        "np_ad_message": "Messaggio pubblicitario",
        "np_ad_break": "Carosello in onda",
        "ad_session_summary_one": "Questa diretta · 1 spot completato",
        "ad_session_summary": "Questa diretta · {n} spot completati",
        "ad_session_airings_one": "1 passaggio completato",
        "ad_session_airings": "{n} passaggi completati",
        "np_banter_strip": "in diretta",
        "np_banter_idle": "I conduttori sono in onda",
        "np_on_air": "In onda",
        "np_now": "Ora in onda",
        "np_next": "Prossimo",
        "np_building": "I prossimi dischi sono in scaletta…",
        "np_no_source": "Nessun disco pronto per ora — ripassa quando la scaletta è pronta.",
        "np_live": "In diretta",
        "clip_saving": "Sto salvando la clip…",
        "clip_copied": "Link copiato — incollalo dove vuoi per condividerlo.",
        "clip_rate_limited": "I registratori stanno ancora montando l'ultima clip — aspetta {s}s e ritocca.",
        "clip_no_audio": "Ancora niente da clippare — lascia suonare la radio un attimo, poi tocca Condividi.",
        "music_share_unavailable": "Prima di condividerlo, lascia finire un brano incluso completo.",
        "clip_error": "La clip non è partita — aspetta un attimo e ritocca Condividi.",
        "clip_copy_prompt": "Copia il link:",
    },
}


def get_copy(super_italian: bool, key: str, default: str = "") -> str:
    """Return the listener-facing string for ``key`` in the active mode.

    Falls back to ``default`` (or empty string) if the key is missing — never
    raises, since a missing copy key should not crash a listener page.
    """
    lang = "it" if super_italian else "en"
    return COPY[lang].get(key, default)


def copy_strings(super_italian: bool) -> dict[str, str]:
    """Return all swappable strings for the active mode.

    Embedded in the /public-status payload so listener.js can read mode-aware
    labels without a second round-trip.
    """
    return dict(COPY["it" if super_italian else "en"])
