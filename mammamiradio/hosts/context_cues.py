"""Temporal and behavioral context signals for uncanny host awareness.

Computes time-of-day vibes, day-of-week energy, listener behavior hints, and
lightly local Italian cultural references. These are injected into banter and
transition prompts so the LLM can produce eerily context-aware lines without
ever touching real user data.

Design rules:
- Never reference specific addresses, real listener names, or private data.
- Use broad regional/cultural cues (weather vibes, football season, food calendar).
- Behavioral hints describe common situations, not observed user behavior.
- Fourth-wall bends are rare (max 1 per ~10 banter segments) and always deniable.
"""

from __future__ import annotations

import datetime
import random

# ---------------------------------------------------------------------------
# Time-of-day show segments
# ---------------------------------------------------------------------------

_SHOW_SEGMENTS: dict[str, dict] = {
    "early_morning": {
        "hours": range(5, 8),
        "name": "L'Alba dei Dannati",
        "vibe": "barely awake, first espresso energy, existential dread about the day ahead",
        "mood_cues": [
            "chi ascolta a quest'ora ha qualcosa da nascondere",
            "il caffè non è ancora entrato in circolo",
            "sveglia brutale, colazione discutibile",
            "i vicini dormono ancora, beati loro",
        ],
    },
    "morning": {
        "hours": range(8, 12),
        "name": "Mattina Pericolosa",
        "vibe": "commute chaos, office dread, school drop-off trauma, caffeine hitting",
        "mood_cues": [
            "bloccati nel traffico, come ogni giorno",
            "la riunione delle nove che poteva essere un'email",
            "già pensando al pranzo e sono le dieci",
            "il collega che parla troppo forte al telefono",
        ],
    },
    "lunch": {
        "hours": range(12, 14),
        "name": "Pausa Pranzo Sacra",
        "vibe": "sacred Italian lunch break, food opinions at maximum intensity, afternoon slump approaching",
        "mood_cues": [
            "la pausa pranzo è un diritto umano",
            "chi mangia al desk è un criminale",
            "il piatto del giorno sospetto della mensa",
            "quel sonnolenza post-pasta che ti distrugge",
        ],
    },
    "afternoon": {
        "hours": range(14, 18),
        "name": "Il Pomeriggio Infinito",
        "vibe": "post-lunch coma, watching the clock, pretending to work, afternoon caffè",
        "mood_cues": [
            "le tre del pomeriggio, l'ora più inutile",
            "stai fingendo di lavorare, lo sappiamo",
            "il secondo caffè della giornata, necessario come l'aria",
            "mancano ancora ore alla libertà",
        ],
    },
    "evening": {
        "hours": range(18, 21),
        "name": "L'Aperitivo Selvaggio",
        "vibe": "work is over, aperitivo hour, cooking dinner, decompression, evening plans",
        "mood_cues": [
            "finalmente liberi, o almeno quasi",
            "l'aperitivo come terapia psicologica",
            "cosa cuciniamo stasera, il dibattito eterno",
            "la TV del vicino troppo alta, come sempre",
        ],
    },
    "late_evening": {
        "hours": range(21, 24),
        "name": "Frequenza Notturna",
        "vibe": "couch time, night owl energy, intimate late-night radio, slightly unhinged",
        "mood_cues": [
            "chi ascolta a quest'ora ha fatto le sue scelte",
            "il divano ti ha inghiottito, non lottare",
            "domani sarà un problema di domani",
            "la notte porta consiglio, o almeno buona musica",
        ],
    },
    "deep_night": {
        "hours": range(0, 5),
        "name": "Radio Fantasma",
        "vibe": "insomnia radio, cursed hours, who is even awake, confessional energy",
        "mood_cues": [
            "le tre di notte, siamo solo noi",
            "se sei sveglio a quest'ora, rispetto",
            "la radio come compagnia nell'insonnia",
            "il mondo dorme, noi no, e forse c'è un problema",
        ],
    },
}

# ---------------------------------------------------------------------------
# Day-of-week energy
# ---------------------------------------------------------------------------

_DAY_VIBES: dict[int, dict] = {
    0: {  # Monday
        "label": "Lunedì — Controllo Danni",
        "energy": "survival mode, weekend hangover, mandatory optimism nobody believes",
        "cues": [
            "lunedì, il giorno che nessuno ha chiesto",
            "il weekend è già un ricordo lontano",
            "cinque giorni alla libertà, ma chi li conta",
        ],
    },
    1: {  # Tuesday
        "label": "Martedì — Il Giorno Invisibile",
        "energy": "the forgotten day, neither beginning nor end, pure limbo",
        "cues": [
            "martedì, il giorno che non esiste",
            "troppo lontani dal weekend in entrambe le direzioni",
        ],
    },
    2: {  # Wednesday
        "label": "Mercoledì — Metà Sentenza",
        "energy": "halfway point, small victory, downhill from here",
        "cues": [
            "metà settimana, si vede la luce",
            "mercoledì, il giorno in cui capisci che ce la puoi fare",
        ],
    },
    3: {  # Thursday
        "label": "Giovedì — Pre-Venerdì",
        "energy": "almost-Friday energy, plans forming, anticipation building",
        "cues": [
            "giovedì, il venerdì dei bugiardi",
            "domani è venerdì, resisti",
        ],
    },
    4: {  # Friday
        "label": "Venerdì — Pre-Game Set",
        "energy": "maximum anticipation, half-working, weekend planning, aperitivo countdown",
        "cues": [
            "venerdì! Il giorno più bello dopo sabato e domenica",
            "stasera si esce, o almeno si finge di voler uscire",
            "l'ultimo giorno di lavoro, se Dio vuole",
        ],
    },
    5: {  # Saturday
        "label": "Sabato — Giornata Libera",
        "energy": "freedom, late wakeup, errands and leisure, no rules",
        "cues": [
            "sabato, il giorno senza sveglia",
            "nessuno ti può dire cosa fare oggi",
            "la spesa al supermercato come sport estremo",
        ],
    },
    6: {  # Sunday
        "label": "Domenica — Slow Spin",
        "energy": "lazy Sunday, existential dread creeping in by evening, family lunch trauma",
        "cues": [
            "domenica, il giorno del pranzo infinito",
            "la sera di domenica, quando la tristezza colpisce",
            "domani si ricomincia, ma per ora il divano è sacro",
        ],
    },
}

# ---------------------------------------------------------------------------
# Lightly local Italian cultural cues (no private data, no exact locations)
# ---------------------------------------------------------------------------

_CULTURAL_CUES = [
    "il vicino che taglia l'erba alle otto di mattina",
    "la moka che fischia come una locomotiva",
    "un altro giorno di bicicletta sotto la pioggia",
    "il motorino del ragazzo sotto casa",
    "il profumo di ragù che sale dalle scale",
    "la signora al balcone che osserva tutto",
    "il bar sotto casa con il caffè perfetto",
    "la discussione eterna su quale pizzeria è migliore",
    "il parcheggio selvaggio sotto il sole",
    "qualcuno sta friggendo qualcosa e tutto il palazzo lo sa",
]

# ---------------------------------------------------------------------------
# Behavioral uncanny cues (common enough to feel like coincidence)
# ---------------------------------------------------------------------------

_BEHAVIORAL_CUES = [
    "ci hai messo in pausa e sei tornato — ti abbiamo aspettato",
    "abbiamo la sensazione che non stai ascoltando davvero, ma va bene",
    "stai facendo finta di lavorare mentre ci ascolti? Rispettabile",
    "se hai alzato il volume per questa canzone, abbiamo notato",
    "ci ascolti da un po' ormai, non ti giudichiamo",
    "se ti sei appena seduto, tempismo perfetto",
    "stai cucinando? Lo sentiamo. No, scherzo. O forse no.",
    "la tua giornata sta andando esattamente come la immaginiamo",
]

# ---------------------------------------------------------------------------
# Fourth-wall bends (rare, always deniable)
# ---------------------------------------------------------------------------

_FOURTH_WALL = [
    "A volte sembra troppo su misura, vero? Coincidenza. Probabilmente.",
    "Se questa canzone ti ha colpito esattamente adesso, è solo caso. Forse.",
    "No, non ti stiamo guardando. Non possiamo. Credo.",
    "Ti sei mai chiesto come facciamo a sapere queste cose? Neanche noi.",
    "Questa è una radio vera. Normale. Niente di strano. Continua ad ascoltare.",
]

# ---------------------------------------------------------------------------
# Seasonal / calendar cues
# ---------------------------------------------------------------------------

_SEASONAL_CUES: dict[int, list[str]] = {
    1: ["gennaio, il mese delle buone intenzioni già infrante", "fa freddo, l'inverno non perdona"],
    2: ["febbraio, il mese più corto ma sembra il più lungo"],
    3: ["marzo, la primavera che promette e non mantiene"],
    4: ["aprile, allergici del mondo unitevi"],
    5: ["maggio, il mese dei ponti e delle scuse per non lavorare"],
    6: ["giugno, l'estate si avvicina, il corpo no"],
    7: ["luglio, chi è ancora in città merita rispetto"],
    8: ["agosto, l'Italia si ferma, e forse dovremmo farlo anche noi"],
    9: ["settembre, si ricomincia, che vuol dire?"],
    10: ["ottobre, la stagione delle castagne e della malinconia"],
    11: ["novembre, il mese del grigio permanente"],
    12: ["dicembre, panettone o pandoro, la guerra infinita"],
}


# ---------------------------------------------------------------------------
# Public API: compute context block for prompt injection
# ---------------------------------------------------------------------------


_NEW_LISTENER_LINES = [
    "Eyyy, qualcuno si è sintonizzato! Benvenuto, chiunque tu sia.",
    "Oh! Abbiamo compagnia. Ciao, ciao. Fai come se fossi a casa.",
    "Sento che qualcuno ci ascolta adesso. Lo sento. Non chiedetemi come.",
    "Ecco, un nuovo arrivo. Siediti, mettiti comodo. Noi siamo già qui da un po'.",
    "Benvenuto nella nostra frequenza. Arrivavi al momento giusto, come sempre.",
]

_FIRST_LISTENER_LINES = [
    "E finalmente qualcuno ci ascolta! Cominciavamo a parlare da soli.",
    "Oh! Il primo ascoltatore! Stavamo per spegnere tutto, giuro.",
    "Qualcuno si è sintonizzato. Allora non trasmettiamo nel vuoto. Che sollievo.",
]


# ---------------------------------------------------------------------------
# Impossible moment lines — pre-written, time/listener-aware, no LLM needed
# ---------------------------------------------------------------------------

_IMPOSSIBLE_LINES: dict[str, list[str]] = {
    "early_morning": [
        "Sveglio a quest'ora? Coraggioso. La moka sta fischiando, lo sappiamo.",
        "L'alba è appena arrivata e tu sei già qui con noi. Rispetto.",
        "Cinque e qualcosa del mattino. Se sei sveglio per scelta, sei un eroe. Se no, condoglianze.",
    ],
    "morning": [
        "Stai andando al lavoro, vero? Lo sentiamo dal traffico nella tua testa.",
        "Mattina. Caffè. Radio. In quest'ordine. Come deve essere.",
        "La riunione delle nove può aspettare. Prima la musica.",
    ],
    "lunch": [
        "Pausa pranzo, eh? Qualsiasi cosa tu stia mangiando, noi approviamo.",
        "L'ora di pranzo è sacra. E tu la stai spendendo con noi. Bravo.",
        "Se stai mangiando al desk, giudicati da solo. Noi non diciamo niente.",
    ],
    "afternoon": [
        "Le tre del pomeriggio. L'ora in cui il tempo si ferma. Ma noi no.",
        "Stai fingendo di lavorare. Lo sappiamo perché anche noi fingiamo di trasmettere.",
        "Il pomeriggio è lungo, ma la playlist è più lunga. Resisti.",
    ],
    "evening": [
        "Sera. Finalmente. La giornata è finita e la musica comincia davvero.",
        "L'aperitivo chiama, ma prima un altro pezzo. Fidati.",
        "Se stai cucinando, alza il volume. Se no, alzalo comunque.",
    ],
    "late_evening": [
        "Notte fonda tra poco. Chi resta sveglio con noi merita una medaglia.",
        "Il divano ti ha inghiottito? Succede. La radio ti tiene compagnia.",
        "A quest'ora le canzoni suonano diverse. Più vere, forse.",
    ],
    "deep_night": [
        "Le tre di notte. Siamo solo noi. E forse qualche fantasma nel corridoio.",
        "Se sei sveglio a quest'ora, hai le tue ragioni. Non chiediamo.",
        "Radio Fantasma in onda. Chi ascolta a quest'ora è dei nostri.",
    ],
}

_IMPOSSIBLE_DAY_LINES: dict[int, list[str]] = {
    0: ["Lunedì. Nessuno voleva questo giorno. Eppure eccoci."],
    1: ["Martedì. Il giorno che nessuno ricorda. Ma noi sì."],
    2: ["Metà settimana. Da qui in poi è tutta discesa. Forse."],
    3: ["Giovedì. Quasi venerdì. Il corpo lo sa già."],
    4: ["Venerdì! Lo senti nell'aria? È libertà. O quasi."],
    5: ["Sabato. Niente sveglie, niente scuse. Solo musica."],
    6: ["Domenica. Il giorno perfetto per non fare assolutamente niente."],
}

_IMPOSSIBLE_LISTENER_LINES: dict[str, list[str]] = {
    "restless_skipper": [
        "Abbiamo notato una certa... impazienza. Questa volta resisti, fidati.",
        "Lo sappiamo che vuoi saltare. Ma questa è quella giusta.",
    ],
    "rides_every_song": [
        "Tu sì che ascolti tutto. Sei la ragione per cui facciamo radio.",
        "Mai un skip. Pazienza infinita. O forse ti sei addormentato?",
    ],
    "ballad_lover": [
        "Sappiamo cosa ti piace. Ecco qualcosa per il cuore.",
        "Un pezzo lento per chi lo merita. Cioè te.",
    ],
    "energy_seeker": [
        "Vuoi energia? Ne abbiamo da vendere. Tieni duro.",
        "Sentiamo che hai bisogno di ritmo. Arriviamo.",
    ],
}


def _current_segment_key(hour: int | None = None) -> str:
    """Return the _SHOW_SEGMENTS key for the given hour, defaulting to deep_night."""
    if hour is None:
        hour = datetime.datetime.now().hour
    for key, seg in _SHOW_SEGMENTS.items():
        if hour in seg["hours"]:
            return key
    return "deep_night"


def generate_impossible_line(
    *,
    segments_produced: int = 0,
    listener_patterns: list[str] | None = None,
    is_new_listener: bool = False,
    is_first_listener: bool = False,
) -> str:
    """Return a pre-written Italian line that feels uncannily aware.

    Uses time-of-day, day-of-week, and optional listener behavior patterns
    to pick a line that sounds like the DJ *knows* the listener. No LLM needed.
    """
    if is_first_listener:
        return random.choice(_FIRST_LISTENER_LINES)

    if is_new_listener and segments_produced < 3:
        return random.choice(_NEW_LISTENER_LINES)

    now = datetime.datetime.now()
    weekday = now.weekday()
    segment_key = _current_segment_key(now.hour)

    candidates: list[str] = []

    # Listener-aware lines (highest priority — these are the "how did they know?" moments)
    if listener_patterns:
        for pat in listener_patterns:
            if pat in _IMPOSSIBLE_LISTENER_LINES:
                candidates.extend(_IMPOSSIBLE_LISTENER_LINES[pat])

    # Time-of-day lines
    candidates.extend(_IMPOSSIBLE_LINES.get(segment_key, []))

    # Day-of-week lines (lower probability — mix in occasionally)
    if random.random() < 0.3:
        candidates.extend(_IMPOSSIBLE_DAY_LINES.get(weekday, []))

    if not candidates:
        candidates = _NEW_LISTENER_LINES

    return random.choice(candidates)


def compute_context_block(
    segments_produced: int = 0,
    listener_paused: bool = False,
) -> str:
    """Build a natural-language context block for LLM banter prompts.

    Returns a formatted string with temporal, cultural, and behavioral cues
    that the LLM can weave into host dialogue. The cues are broad enough
    to feel like coincidence, never specific enough to be surveillance.

    Args:
        segments_produced: how many segments have aired (for fourth-wall pacing)
        listener_paused: whether the listener recently paused and resumed
    """
    now = datetime.datetime.now()
    weekday = now.weekday()
    month = now.month

    segment = _SHOW_SEGMENTS[_current_segment_key(now.hour)]
    day = _DAY_VIBES[weekday]

    # Pick cues (randomize to avoid repetition across segments)
    time_cue = random.choice(segment["mood_cues"])
    day_cue = random.choice(day["cues"])
    cultural_cue = random.choice(_CULTURAL_CUES)
    seasonal_cues = _SEASONAL_CUES.get(month, [])
    seasonal_cue = random.choice(seasonal_cues) if seasonal_cues else ""

    # Behavioral cue: only sometimes (30% chance)
    behavioral_cue = ""
    if listener_paused:
        behavioral_cue = _BEHAVIORAL_CUES[0]  # the pause-specific one
    elif random.random() < 0.3:
        behavioral_cue = random.choice(_BEHAVIORAL_CUES[1:])

    # Fourth-wall: rare (once every ~10 segments, and only if >5 segments in)
    fourth_wall = ""
    if segments_produced > 5 and random.random() < 0.1:
        fourth_wall = random.choice(_FOURTH_WALL)

    # Weekend vs weekday flag
    is_weekend = weekday >= 5

    lines = [
        f"SHOW SEGMENT: {segment['name']}",
        f"Time vibe: {segment['vibe']}",
        f"Day energy: {day['label']} — {day['energy']}",
        f"It is {'weekend' if is_weekend else 'a weekday'}, {now.strftime('%H:%M')}.",
        "",
        "AVAILABLE CONTEXT CUES (use at most ONE naturally, don't force it):",
        f'- Time-of-day: "{time_cue}"',
        f'- Day-of-week: "{day_cue}"',
        f'- Local vibe: "{cultural_cue}"',
    ]

    if seasonal_cue:
        lines.append(f'- Seasonal: "{seasonal_cue}"')
    if behavioral_cue:
        lines.append(f'- Listener hint: "{behavioral_cue}"')
    if fourth_wall:
        lines.append(f'- Fourth wall (USE SPARINGLY, max once per session): "{fourth_wall}"')

    lines.append("")
    lines.append(
        "CONTEXT RULES: These cues should feel like casual observations, not data readouts. "
        "Use at most ONE per banter segment. The goal is a subtle 'are they talking to me?' feeling, "
        "never 'they are watching me'. If a cue doesn't fit naturally, skip it entirely."
    )

    return "\n".join(lines)
