# Welcome Clips

Pre-generated welcome clips that play when the station starts.
The DJ "interrupts" the broadcast to greet the listener.

## Generating clips

Run from the project root with the venv active:

```bash
python -c "
import asyncio
from mammamiradio.audio.tts import synthesize
from pathlib import Path

clips = [
    ('marco_welcome_1.mp3', 'it-IT-GianniNeural', 'Eyyy, qualcuno si e collegato! Benvenuto, benvenuto!'),
    ('marco_welcome_2.mp3', 'it-IT-GianniNeural', 'Eccolo! Un nuovo ascoltatore! Che bello, che bello!'),
    ('giulia_welcome_1.mp3', 'it-IT-ElsaNeural', 'Benvenuto... vediamo cosa ci hai portato oggi.'),
    ('giulia_welcome_2.mp3', 'it-IT-ElsaNeural', 'Oh, qualcuno si e sintonizzato. Finalmente.'),
]

async def gen():
    out = Path('mammamiradio/assets/demo/welcome')
    for name, voice, text in clips:
        await synthesize(text, voice, out / name)
        print(f'Generated: {name}')

asyncio.run(gen())
"
```

These clips are Italian-only by design (matches station identity).
