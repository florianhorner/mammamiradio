"""Stock fallback copy for the AI hosts: chaos stock lines and ad-break bumpers.

Extracted verbatim from ``hosts/scriptwriter.py`` (god-module split). Pure data —
the canned lines hosts fall back to for chaos beats and ad breaks. Self-contained
apart from the ChaosSubtype enum. Reloaded ahead of the scriptwriter facade by
/api/hot-reload so edits here take effect without a stream gap.
"""

from __future__ import annotations

from mammamiradio.core.models import ChaosSubtype

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
