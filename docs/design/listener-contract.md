# Listener interaction contract

The listener surface is a radio-station website with one behavioral rule above
all others: an interaction does only what it visibly promises. Audio starts from
a play affordance, form actions return visible feedback, and server station
identity wins over browser cache state.

The visual composition remains defined by [the design system](system.md). This
document owns the interaction and state contract for `/` and `/listen`.

## Playback controls

| Control | Role | Resting state | Active state |
|---|---|---|---|
| `#nav-cta` | Primary play/pause toggle | Visible **Listen Now**, `aria-pressed="false"`, action-announcing label. When the station is stopped it becomes a disabled **Station paused** status; listeners cannot override an operator stop. | Visible **Pause**, `aria-pressed="true"`, label **Pause station** |
| `#np-play` | Compact play/pause toggle | Play icon and **Play** label | Pause icon, blue playing state, `aria-pressed="true"` |
| `#hero-play` | Hero play/pause affordance | Mode-aware listen copy, `aria-pressed="false"` | Visible mode-aware pause copy, `aria-pressed="true"`, and the same audio element as the other play controls |
| `#hero-palinsesto` | Navigation | Scrolls to the schedule | Never starts audio |
| `#share-clip-btn` | Clip action | Saves and shares the current clip | Never starts audio; every failure gives a retry path |
| `#pwa-install-btn` | Install action | Opens the captured browser install prompt | Never starts audio |

Only the three explicit playback controls may call the playback toggle. There
is no document-level click or touch listener that unlocks audio. A dedication,
anchor link, share action, or install action must never surprise the listener
with sound.

All three playback controls share the same pending/playing state. While a play
request or bounded reconnect is pending, their next action is Pause and cancels
that intent. At most one reconnect timer may exist. A listener pause clears it,
so a delayed retry can never restart sound after the listener opted out. When
`session_stopped` is true, all three controls are disabled until the public
status reports the station live again.

The listener interaction smoke intercepts the browser's actual request to
`/stream` and requires that playback intent to leave the page in under two
seconds. Real process-start and first-byte delivery remain owned by
`make launch-smoke`; the two gates intentionally test different boundaries.

## Dedication form

`#req-name` is optional. `#req-msg` is required and points to
`#request-sent` with `aria-describedby`.

| State | Required behavior |
|---|---|
| Empty submit | Block the request, focus the message field, set `aria-invalid`, and show mode-aware, actionable copy in the polite live region. Keep the fields visible so the listener can fix it immediately. |
| Editing after an empty submit | Clear the validation message and `aria-invalid` once the message contains non-whitespace text. |
| Submitting | Disable duplicate submits and bound the request to eight seconds. |
| Success | Show the receipt in the dedication card. The form fields may hide, but the form ancestor must remain rendered so its live-region receipt is actually visible and announced. |
| Declined / queue full | Show mode-aware warm house copy with a concrete retry path; never render a raw backend error. Restore the fields without erasing the listener's message. |
| Network failure | Explain that the connection failed and tell the listener to retry. Restore the form after the bounded message interval without erasing the listener's message. |

## Language policy

The Italian/English mix is intentional. Super Italian Mode is a personality
dial, not a blanket translation switch.

- Headlines, section names, and brand idioms remain Italian in both modes.
- Buttons, placeholders, validation, and dynamic utility labels come from
  `mammamiradio.web.ui_copy` and follow the active mode.
- Under the default English page language, persistent Italian phrases carry
  `lang="it"` on the nearest useful element so assistive technology can switch
  pronunciation without changing the visual copy.
- The admin remains English-first and is outside this listener contract.

Every new `ui_copy` key must exist in both `en` and `it`.

## Station identity source

The server is authoritative. The browser resolves dynamic station identity in
this order:

1. `/public-status.identity.station_name`
2. `/public-status.brand.station_name`
3. `localStorage.stationName`, only when both server fields are absent
4. the server-rendered document title
5. the built-in `Mamma Mi Radio` fallback

The initial wordmarks and document title are rendered from the server `brand`
object. After every successful status poll, listener JavaScript writes the
server-resolved name back to `localStorage`. That write-through is the explicit
synchronization boundary for the duplicated cache: an admin-written or stale
value can help before status arrives, but it cannot override a server-provided
identity.

If the server itself reports the wrong name, fix its supported configuration
source (`radio.toml`, environment, or add-on options). Browser cache surgery is
not a server-identity fix.

## Automated guardrails

The listener invariant tests pin required fields, action labels, scoped
playback, per-element language, receipt visibility, and identity precedence.
Run them with:

```bash
.venv/bin/python -m pytest \
  tests/web/test_listener_mobile_invariants.py \
  tests/web/test_player_smoke_contract.py \
  tests/web/test_ui_copy.py -q
```

With a local station already serving the listener page, run the deterministic
browser flow with:

```bash
make player-smoke
# Alternate server or preinstalled/offline CLI:
make player-smoke PLAYER_SMOKE_URL=http://127.0.0.1:8141 \
  PLAYWRIGHT_CLI="$HOME/.codex/skills/playwright/scripts/playwright_cli.sh"
```

The smoke loads real local HTML and listener assets. It reads the real
authoritative station identity once, then freezes `/public-status` to a
deterministic payload carrying that identity. Public dedication reads/writes
and the media response are mocked, so it neither adds a dedication nor opens a
never-ending connection to the running station. It checks visible identity and
stale-cache repair, no audio from form interactions, localized empty, success,
rate-limit, queue-full, decline, and network feedback, honest play/pause state,
and an intercepted `/stream` request under two seconds. All waits are bounded
and the isolated browser session is closed on exit.

Player QA on `/` remains the manual release gate for listener-facing changes.
