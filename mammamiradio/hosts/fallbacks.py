"""Stock fallback copy for the AI hosts: chaos stock lines and ad-break bumpers.

Extracted from ``hosts/scriptwriter.py`` (god-module split). Holds the canned
lines and small selectors hosts use for chaos beats and ad breaks. Self-contained
apart from the ChaosSubtype enum. Reloaded ahead of the scriptwriter facade by
/api/hot-reload so edits here take effect without a stream gap.
"""

from __future__ import annotations

import random

from mammamiradio.core.models import ChaosSubtype

# Keep the Italian map under its long-standing public name.  Scriptwriter uses
# ``chaos_stock_lines`` below so this copy is selected only when the station is
# actually speaking Italian in Super Italian Mode.
CHAOS_STOCK_LINES: dict[ChaosSubtype, list[str]] = {
    ChaosSubtype.FOURTH_WALL: [
        "Aspetta. Hai sentito anche tu il momento in cui siamo diventati una frase dentro una macchina?",
        "No, no, continua. Se lo dici piano sembra ancora radio.",
        "Perfetto. Musica prima che qualcuno legga il prompt.",
    ],
    ChaosSubtype.ABANDONED_STORM: [
        "Allora io volevo dire che—",
        "No, perche il punto vero e— aspetta, non quello, l'altro—",
        "Musica. Subito. Prima che finiamo una frase.",
    ],
    ChaosSubtype.IMPOSSIBLE_RECALL: [
        "Mi torna in testa quel pezzo di prima, quello sentito earlier, "
        "come se fosse passato qui con le scarpe bagnate.",
        "Sì. Non nominarlo troppo forte o rientra dalla finestra.",
        "Troppo tardi. Andiamo avanti.",
    ],
    ChaosSubtype.ICON_MOMENT: [
        "Questa e esattamente la regola di Zio Bravissimo da Catania Due: mai spiegare, sempre indicare il soffitto.",
        "Finalmente qualcuno lo dice in radio.",
        "E adesso musica, per rispetto del soffitto.",
    ],
    ChaosSubtype.URGENT_INTERRUPT: [
        "Madonna, ma quante volte te lo dobbiamo dire?",
        "Il timer è scaduto. SCADUTO. Non è una suggestione.",
        "Dai, muoviti. Ora. Senza aspettare.",
    ],
}

# Normal Mode is English-led even when the station's configured language is
# Italian.  The subtype anchors are intentionally distinct, so a degraded
# Chaos break still lands as the beat the operator selected rather than generic
# emergency banter.
CHAOS_NORMAL_STOCK_LINES: dict[ChaosSubtype, list[str]] = {
    ChaosSubtype.FOURTH_WALL: [
        "Hold on. Did we just hear the fourth wall breathe?",
        "Don't look at it. If we name the machinery, it gets ideas.",
        "Good. Music, before the studio notices us.",
    ],
    ChaosSubtype.ABANDONED_STORM: [
        "I was about to make one clean point—",
        "No, the storm got there first, and now every sentence is sideways—",
        "Music. Immediately. Before the weather finishes the argument.",
    ],
    ChaosSubtype.IMPOSSIBLE_RECALL: [
        "That song from earlier is back in the room, amici, grazie, wearing wet shoes.",
        "Don't say its name too loudly or it comes through the window.",
        "Too late. Keep it moving.",
    ],
    ChaosSubtype.ICON_MOMENT: [
        "That is exactly the rule of the icon, amici — never explain it, just point at the ceiling, grazie.",
        "Finally, someone said it on the radio.",
        "Music now, out of respect for the ceiling.",
    ],
    ChaosSubtype.URGENT_INTERRUPT: [
        "How many times do we have to say it?",
        "The timer is done. Done. This is not a suggestion.",
        "Move. Now. No waiting around.",
    ],
}

_CHAOS_SOLO_RECOVERY_LINES: dict[bool, list[str]] = {
    False: [
        "The chaos is real, but we can land this.",
        "Music. We keep moving.",
    ],
    True: [
        "Il caos è reale, ma chiudiamo il punto.",
        "Musica. Continuiamo.",
    ],
}


def chaos_stock_lines(
    *,
    super_italian_mode: bool,
    station_language: str,
) -> dict[ChaosSubtype, list[str]]:
    """Select Chaos recovery copy for the station's active spoken mode."""
    if super_italian_mode and station_language == "it":
        return CHAOS_STOCK_LINES
    return CHAOS_NORMAL_STOCK_LINES


def chaos_solo_recovery_lines(
    *,
    super_italian_mode: bool,
    station_language: str,
) -> list[str]:
    """Select the complete one-host recovery exchange for the spoken mode."""
    return _CHAOS_SOLO_RECOVERY_LINES[super_italian_mode and station_language == "it"]


AD_BREAK_INTROS = [
    "E ora... un messaggio dai nostri sponsor!",
    "Ma prima, una pausa pubblicitaria!",
    "Restate con noi, torniamo dopo questi messaggi!",
    "E ora, le cose importanti della vita... la pubblicità!",
    "Un attimo di pausa per i nostri amici commerciali!",
    "Ecco a voi... la pubblicità! Non cambiate stazione!",
]

AD_BREAK_OUTROS = [
    "Bene, siamo tornati!",
    "Eccoci di nuovo! Vi siete persi?",
    "E torniamo alla musica, finalmente!",
    "Siamo ancora qui! Non siamo scappati!",
    "Ok, basta pubblicità. Per ora.",
    "Torniamo a noi! Dove eravamo rimasti?",
]

# These wrapper inventories are intentionally English-led in Normal Mode.  The
# Italian ``AD_BREAK_*`` lists stay available under their historical names for
# compatibility with callers that only need the Super Italian copy (and for
# existing hot-reload/shape tests).  New producer code should use the selectors
# below so the active spoken mode is explicit at the final TTS boundary.
AD_BREAK_NORMAL_INTROS = [
    "And now... a word from our sponsors, amici!",
    "But first, a quick ad break — stay with us, amici!",
    "Stay with us, amici — we'll be right back after these messages!",
    "A short commercial pause, amici — then we're back to the music!",
    "One brief word from our sponsors, amici — grazie for staying here!",
]

AD_BREAK_NORMAL_OUTROS = [
    "We're back, amici — right into the music!",
    "Welcome back, amici — let's get moving.",
    "Back to the music, finally — grazie for staying with us!",
    "The ads are done, amici — where were we?",
    "And we're back — thanks for hanging on, amici!",
]


def select_ad_break_intro(super_italian: bool) -> str:
    """Return one mode-appropriate spoken ad-break intro.

    The selector owns random choice so producer call sites cannot accidentally
    choose from the Italian compatibility inventory in Normal Mode.
    """
    pool = AD_BREAK_INTROS if super_italian else AD_BREAK_NORMAL_INTROS
    return random.choice(pool)


def select_ad_break_outro(super_italian: bool) -> str:
    """Return one mode-appropriate spoken ad-break outro."""
    pool = AD_BREAK_OUTROS if super_italian else AD_BREAK_NORMAL_OUTROS
    return random.choice(pool)


def select_ad_promo_tag(super_italian: bool) -> str:
    """Return the short compliance tag spoken before a sponsored spot."""
    if super_italian:
        return "Messaggio promozionale."
    return "A word from our sponsors, amici."
