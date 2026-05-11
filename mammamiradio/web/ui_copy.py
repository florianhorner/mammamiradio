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
        "listen_now_aria": "Listen now",
        "schedule_button": "Schedule",
        "install_app": "Install app",
        "footer_listen": "Listen",
        # Listener page — stat labels under hero
        "stat_airtime": "On Air Today",
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
        "form_submit": "Send with a kiss",
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
        "seg_default": "On Air",
        # Now-playing strip + palinsesto inline strings rendered by listener.js
        "np_paused": "Paused",
        "np_welcome": "Welcome aboard",
        "np_ad_message": "Sponsored message",
        "np_banter_strip": "in conversation",
        "np_banter_idle": "The hosts are on air",
        "np_on_air": "On Air",
        "np_now": "On now",
        "np_next": "Next",
        "np_building": "Building schedule…",
        "np_live": "Live",
    },
    "it": {
        "listen_now": "Ascolta Ora",
        "listen_now_aria": "Ascolta ora",
        "schedule_button": "Il Palinsesto",
        "install_app": "Installa app",
        "footer_listen": "Ascolta",
        "stat_airtime": "In onda oggi",
        "stat_tracks": "Tracce in playlist",
        "stat_hosts": "I conduttori",
        "tuning_in": "Stiamo accendendo la radio…",
        "schedule_loading": "Il palinsesto sta arrivando…",
        "waiting_dedication": "Aspettiamo la prima dedica della sera…",
        "form_name_label": "Nome (opzionale)",
        "form_name_placeholder": "Come ti chiami? (opzionale)",
        "form_message_label": "Messaggio o richiesta musicale",
        "form_message_placeholder": "Cara Radio, vorrei dedicare una canzone a…",
        "form_submit": "Spedisci con un bacio",
        "now": "adesso",
        "minutes_ago": "min fa",
        "hours_ago": "h fa",
        "seg_music": "Musica",
        "seg_banter": "Banter",
        "seg_ad": "Sponsorizzato",
        "seg_news": "Notizie",
        "seg_jingle": "Jingle",
        "seg_welcome": "Benvenuto",
        "seg_default": "In onda",
        "np_paused": "In pausa",
        "np_welcome": "Ben arrivato",
        "np_ad_message": "Messaggio pubblicitario",
        "np_banter_strip": "in diretta",
        "np_banter_idle": "I conduttori sono in onda",
        "np_on_air": "In onda",
        "np_now": "Ora in onda",
        "np_next": "Prossimo",
        "np_building": "In costruzione…",
        "np_live": "In diretta",
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
