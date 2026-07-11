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
        # Listener page — stat labels under hero
        "stat_airtime": "On Air Today",
        "casa_moments_title": "Live from your home",
        "casa_moment_airing": "on air now",
        "casa_moment_minutes_ago": "{m} min ago",
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
        "form_success_song": "Song request received! The hosts will cue it soon.",
        "form_success_shoutout": "Dedication received! The hosts will read it soon.",
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
        "stat_airtime": "In onda oggi",
        "casa_moments_title": "In diretta da casa tua",
        "casa_moment_airing": "in onda ora",
        "casa_moment_minutes_ago": "{m} min fa",
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
        "form_success_song": "Richiesta ricevuta! I conduttori metteranno presto la canzone in scaletta.",
        "form_success_shoutout": "Dedica ricevuta! I conduttori la leggeranno presto.",
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
