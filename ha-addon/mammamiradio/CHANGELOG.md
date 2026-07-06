# Changelog

## Unreleased

### Fixed

- **Recovery audio now gets in before the add-on slows down retries.** If segment generation fails repeatedly, the station queues its backup audio first and only then backs off the retry loop, so listeners still get cover audio during a rough provider or download stretch. Resume and idle bridges also share the same final emergency-tone fallback when no canned clip or cached song is ready.
- **Backup audio now sounds like the station instead of bare silence.** When a segment fails and no recorded recovery clip is available, the add-on now plays a short branded recovery sweeper before using the last-resort silence placeholder, so provider or download trouble feels intentional instead of like dead air.
- **The public "Up Next" schedule no longer shows songs that were never actually queued.** When the render queue was empty, `/public-status`, the listener page, and the admin producer desk used to see a guessed lineup pulled from the rotation pool, shown as if it were real. Those public schedule surfaces now list only segments that are truly ready to air; when nothing is ready yet, the listener page and the admin producer desk each show one honest status line — distinguishing "still getting the next thing ready" from "no music source configured" and "station paused" — instead of padding out four fake placeholder rows. The v1 integration endpoint still exposes scheduler guesses as `up_next` rows with `predicted: true`, so integrations that need the same rendered-only queue should filter to `predicted === false`.

## 2.16.0

## 2.15.0 - 2026-07-06

### Added

- **Impossible Hours can now opt into specific Home Assistant events.** `radio.toml` supports commented `[[home.radio_event]]` rules that promote explicit state, attribute, or numeric-threshold changes into next-break directives or evening running-gag material without broadening the ambient Home Assistant prompt context.
- **New releases can now introduce themselves on air.** A packaged release beat can give the hosts a bounded cold-open campaign after an update, counted only when a real listener receives streamed audio. The station can also bring back a recently rendered music segment after restart so the first listen reaches live programming faster.
- **Listener dediche can now reject configured real-name matches before they reach the hosts.** Operators can keep `blocked_names` under `[moderation]` in `radio.toml`; it ships empty, but when filled it catches names case-insensitively and accent-insensitively without echoing the private list back to listeners.
- **The admin panel now shows where estimated AI spend is going.** Motore's cost card keeps the single session total, then splits it into host scripts, transitions, ad scripts, post-air memory extraction, and voice synthesis. Older sessions that only have the old aggregate counter show an honest "not available yet" note instead of pretending every category is zero, and unknown model prices are still flagged as estimates.
- **New "Guest host" option.** On by default — the rotating guest host stays in the line-up. Turn it off to keep the show to your regular hosts only. Takes effect after the add-on restarts.

### Changed

- **Durable listener memories now wait for a clean banter stream.** New listener theories and song reaction cues are extracted afterward from the final streamed script, so queued, skipped, fallback, or half-sent banter no longer writes durable listener or song-cue memory.
- **The two language modes now do what they say.** With Super Italian Mode on, the hosts speak fully in Italian — no more English asides slipping through. In the default mode, the hosts now speak about 70% English with real Italian moments — including whole Italian sentences — and news flashes and ads follow the same mix rather than staying all-Italian inside an English-led show.
- **Hosts push through a long thought instead of shrugging into filler.** When a host break runs longer than the writing budget allows, the station retries once with more room — first with its main writer, then with the backup — instead of quietly airing a generic filler line. The budget grew to match how much the hosts actually have to say, each attempt gets proportional breathing room under a hard time limit, and the music never waits on a chatty host.
- **The Configuration tab is easier to scan.** The first screen now keeps the station, Home Assistant, AI quality, admin token, personality, and sound controls together. The Jamendo client ID moves behind Home Assistant's optional configuration disclosure for new installs and installs where the saved key is absent; existing installs that already saved a blank value may still show it until that saved option is cleared.
- **Generated ad and imaging layers stop being remade every time.** The station now reuses its own generated music beds, ambient textures, motifs, and transition stings from the local cache when their inputs match. Repeated ad breaks and host transitions stay lighter on Raspberry Pi-class hardware, while the ambient layers keep a small rotation so the station does not sound like one identical loop.
- **Banter is shorter by default.** Hosts keep most breaks to a quick beat between songs, saving longer breaks for moments that earn them — a home-event reaction, a listener request, an operator course change, or Festival Mode.

### Fixed

- **The add-on logs stay calmer when providers or downloads fail safely.** Ad promo tags now use the configured ad voice engine instead of handing ElevenLabs IDs to Edge TTS, Azure and ElevenLabs auth/config failures fall back once and then stay on the Edge fallback for the session, direction-download failures no longer dump traceback noise for ordinary YouTube blocks, and Home Assistant state pushes are smoothed into ordered writes.
- **Hans Günther now stays a cameo instead of taking over a host break.** The add-on recognises short or oddly punctuated Hans tags as guest-host attempts, drops them when the cameo gate is closed, and falls back to a full regular-host exchange if needed.
- **Your pacing settings now stick.** The Diretta sliders in the admin panel — songs between host breaks, songs between ad breaks, and ads per break — used to reset to their defaults after a restart. They are now saved and restored, and the three values also appear on the add-on Configuration screen. If a save can't be written the panel says so and leaves the current setting untouched instead of half-applying it.
- **Restart handoff scratch files no longer build up in `/data/cache`.** On startup the add-on now prunes stale restart-handoff `.tmp` files left behind by an interrupted update, while keeping the published manifest and finished music handoff files intact.
- **Bad request bodies now fail gently instead of looking like an add-on fault.** Admin and listener write endpoints that expect request details now share one parser: empty, malformed, or wrong-shaped bodies return a calm `422` response with `ok: false` and a human message, instead of leaking raw server errors or inconsistent 400/200 responses.
- **"Clear pool" now actually empties the rotation.** The clear-pool button in the Rotazione tab used to fail with an error; it now clears the whole pool, and the song that's already playing finishes first so there is no dead air.
- **Admin panel visual cleanups.** The "Shuffle" button shows its proper icon, the host personality sliders fill as a clean thin track instead of a tall colour block, the doubled hairline under section headers is now a single rule, and the gold accent on the live console runs cleanly into its rounded corners.

### Security

- **Provider key fields are gone from the add-on Configuration tab.** API credentials now live only in `/config/secrets.env` (written for you by the admin setup panel), so on a fresh install add-on diagnostics like `ha addons info` never see them. Keys saved through the old Configuration-tab fields move into the secrets file automatically the first time the updated add-on starts — nothing to re-enter. Older saved values can linger in Home Assistant's stored add-on settings until you open the add-on's Configuration tab and press Save once.
- **Listener requests are harder to spoof behind Home Assistant ingress.** The station now rate-limits requests by the closest real listener address that Home Assistant forwards, so a forged `X-Forwarded-For` entry cannot move a listener into someone else's bucket. Direct callers are still bucketed by their direct connection, and raw addresses remain HMAC-only.

## 2.14.1 - 2026-06-21

### Added

- **A new On-air media player push option, and a real media player to go with it.** Mamma Mi Radio now has a Home Assistant integration you can install from HACS that registers the station as a proper, controllable media player (play, stop, next). If you install it, turn this new option off so the add-on stops pushing its own basic media player and lets the integration take over — your sensors keep working either way.
- **Steer the music by mood, and the hosts notice.** Tap an era or a vibe in the admin panel and the station re-aims its programming toward it. The on-air host picks it up as a feeling rather than a setting — "someone's got a soft spot for the 80s tonight" — so it sounds like the station read the room. It sticks across a restart, clears with one tap back to automatic, respects your banned songs, and never interrupts what's playing.

### Changed

- **The "ban a song" controls are easy to find now — and a slip is undoable.** Banning used to hide behind a tiny ✕ that only appeared on row hover, so on a phone or tablet it was unreachable and on a computer it was easy to miss. Every song in the rotation now shows a clear red "✕ Ban" button you can always see and tap, the "Banned" button carries a count so you can see at a glance how many songs are banned and where to manage them, a short note explains how it works until your first ban, and an "Undo" prompt appears right after a single ban so an accidental tap is one tap away from being lifted.

### Fixed

- **Host chatter beds over the right song now.** When the hosts talk over the tail of a track, the music underneath is the song that actually just played — even right after the station recovers from a quiet stretch by reaching for a backup track.
- **The admin producer desk no longer traps the phone screen under its header.** On mobile, the live console and tab bar now scroll away with the page instead of staying pinned over the work area. Desktop keeps the pinned producer deck, but phones get the vertical room they need to use Scaletta, Rotazione, and Motore without the upper controls covering the view.
- **Leftover working audio no longer piles up in `/data`.** The station writes short-lived scratch files while it builds each segment and clears them as it goes, but a restart at the wrong moment could strand some on disk, and over many restarts they slowly added up. The add-on now sweeps away any stale leftovers when it starts, so its storage stays tidy on its own.
- **Sharing a clip can't briefly switch off its own slow-down guard.** The "share this moment" button limits how often it can be tapped. If one tap failed to find audio at the same instant another succeeded, cleaning up after the failed one could wipe the successful tap's record and let the next tap skip the wait. The cleanup now only ever clears its own tap, so the gentle pacing always holds.

### Removed

- **The orphaned `/live` operator page is gone.** The old phone-only control room had no entry point from the listener page, admin panel, or add-on, so the hidden route and standalone template have been removed. The admin panel remains the supported operator surface.

### Security

- **The handler that serves the app's icons and manifest is locked down tighter.** It now refuses any request that tries to reach outside the app's own static-asset folder — including links inside that folder that point elsewhere — so it can only ever return the bundled web files.

## 2.14.0

## 2.13.0

### Security

- **Track IDs in the admin queue can no longer slip code into a click action.** A song or queue entry whose ID contained a double-quote could previously break out of a button's click action in the producer desk's Live Queue and inject unwanted markup — a cross-site scripting gap reachable from the admin panel. Track IDs now ride on each row as escaped data attributes and are read back through a single shared click handler, so an ID's contents can never land inside a JavaScript action again.

### Changed

- **On-Air Sound is now off by default (studio-clean).** This add-on option applies a deliberately subtle FM-style colouring that is often imperceptible on good speakers, so the station now ships clean. Turn it on from the add-on options or the admin Engine Room if you want the FM character. The option's description now explains what it does and how subtle it is.

- **OpenAI script fallback now matches the quality dial.** Anthropic remains the primary scriptwriter, but OpenAI fallback now uses `gpt-5.5` for creative copy in balanced/premium and `gpt-5.4-mini` for fast transitions and economy instead of the older small-model fallback.

- **Clearer confirmation when you change the station's character — and a record of it.** Turning Super Italian Mode off now says, in plain words, that the hosts switch to English-first (with Italian flavour) and the listener page changes to English, instead of a cryptic "Italian mode: OFF". Every station-wide change you make in the Engine Room — language, Chaos, Festival, AI quality, On-Air Sound — is recorded (best-effort) to the add-on's Show Memory at `/data/cache/ledger/`, so you can later see what you changed and when, while auditing why the station suddenly sounded different.

### Fixed

- **A station-wide setting now fails cleanly if it can't be saved.** Turning Super Italian or Festival on or off used to change the live station before saving your choice — so if the add-on couldn't write to `/data` (full or read-only), the change applied but wouldn't survive a restart, and Festival had already cleared the buffered queue. Every Engine Room toggle now saves first and only changes the station if the save succeeds, so a failed save leaves everything exactly as it was.

- **A song you request no longer plays twice.** When a listener asked the hosts for a track, it could occasionally air a second time a few minutes later: the request claimed the "play next" slot once when it finished downloading and again when the hosts gave its dedication. A requested song is now pinned exactly once, so it airs a single time alongside its shout-out.

- **Ad breaks no longer have a strange "swirling" sound.** The On-Air Sound colouring included a moving effect that smeared the radio-static sound between an ad's spoken lines into an odd, headphone-like swirl. That effect has been removed; the colouring is now gentle tone shaping with no swirl.

- **The connected-home hello now retries if the host script falls back.** If AI banter generation fails and the station uses its stock banter instead, the first connected-home moment stays queued for a later real host break instead of being marked done. Homes with safe labels but no room metadata can still qualify for the hello. A running joke that didn't make it to air for the same reason keeps its turn too, instead of quietly going on cooldown.

- **Direct `/admin` access from your home network now works.** If you open `http://<pi-ip>:8000/admin` in a browser on your local Wi-Fi, the admin panel loads without needing a token. If you configured a custom `admin_token` in the add-on options, that token is still enforced. From outside your home network, `/admin` returns 403.

### Added

- **The Engine Room tells you when the station is running on rescue.** A new Queue rescue row shows how often the station has to bridge a gap with cached, canned, or stand-in audio when fresh content isn't ready in time. Now and then is normal; if it starts happening repeatedly (three times in fifteen minutes) the row flips to a warning, so you can tell a station that's genuinely live from one that only sounds live because something keeps filling the gaps. It also shows the last bridge and how long the queue has been empty right now.

- **The host gives your connected home a warm first hello.** The first time the station has a clear read on your Home Assistant home, the host slips one or two real details about it into a break — naturally, like a DJ who just noticed where you are, not by reading off a sensor list. It lands once, then the host settles back into the usual mix. The Engine Room shows how much home context the host is working with and whether that first moment has aired yet.

- **Admin playlist and search pagination** — Large rotations no longer over-render in the Producer Desk. Status, playlist, and search APIs expose bounded windows with load-more metadata, while artwork from Apple charts, web search results, and listener-request downloads is preserved through queueing.

- **Real album covers on the now-playing screen.** When a song is on, the phone
  lock screen, CarPlay, Control Center, and the Home Assistant media card now show
  the real album artwork instead of the station logo, and it follows each track.
  Chart songs carry their cover from the chart feed; searched/added and listener-
  requested songs get their cover looked up automatically. No cover found falls back
  cleanly to the station logo — never a broken image.

- **AI quality dial replaces the model dropdown.** The old "AI Model" option is now
  a Premium / Balanced / Economy quality dial. Pick the experience you want and the
  station chooses the right model for each job automatically — no model names to
  track, and it keeps working when new models ship. Existing add-ons update with no
  change in behavior: if `/data/options.json` still contains the removed
  `claude_model` option, it is honored as the legacy fast-model override until you
  save the new quality profile. The dial is also in the admin Engine Room and
  switches live without a restart.

- **Share a whole moment, not just thirty seconds.** The Share button now always
  copies the clip link to your clipboard (alongside the native share sheet). Clips
  of host banter and ads capture the full segment instead of a fixed 30-second
  window, with a short grace period so you can still grab a great ad a beat too
  late; music clips stay at 30 seconds. When the station is busy, the Share button
  speaks plainly ("the tape decks need a moment — give them a few seconds and tap
  again") instead of a technical error.

- **Expanded TTS voice routing** — hosts, sweepers, station IDs, and ad
  character voices can now use Edge, OpenAI, Azure Speech, or ElevenLabs TTS
  with per-voice Edge fallbacks. Add-on options now include Azure Speech and
  ElevenLabs credentials for premium voice mixes without editing secrets into
  `radio.toml`.

- **Voice audition clips** — `scripts/audition_tts_voices.py` can now generate
  local MP3 samples plus a manifest for the configured cast and the built-in
  Edge/OpenAI/Azure catalogs. Missing provider credentials are reported as
  skipped so auditions are not confused with runtime Edge fallback.

- **The admin Engine Room now tells you exactly what the station is doing** — the header badge shows "On Air" when music or hosts are streaming, "Paused" when you've stopped it deliberately, and "Error" when a task has died and needs attention. Provider chips now distinguish "Backup active" (primary is down) from "Auto-recovering" (transient error, will self-heal), and show a plain-English reason plus a retry countdown. Silence while listeners are connected now surfaces as a blocked state immediately.

- **The admin now shows what happened to a listener request after the hosts handled it.** A "Recently handled" section appears below the Pending queue for up to 5 minutes, showing each request with a status badge — "Sent to hosts" (blue) when the hosts picked it up, or "Song not found" (amber) when the requested track could not be downloaded.

- **Jamendo rotation depth now defaults to 200 tracks.** The add-on's bundled
  `radio.toml` sets `jamendo_limit = 200`, and advanced deployments can override
  it with `JAMENDO_LIMIT` (`1`-`200`) to tune Jamendo API result depth.

- **Home Assistant context now adapts to each home.** The add-on scores prompt-safe
  entities from the full Home Assistant state snapshot instead of only using a
  hardcoded apartment list. Location, camera, alarm, and free-text helper
  entities plus secret-shaped attributes are filtered before prompt assembly;
  who's home stays as simple home/away (never location) so the hosts can still
  welcome you back and notice an empty house. The admin Engine Room shows what
  was selected plus privacy filter counts.

- **Music Assistant now-playing contract** — a new read-only endpoint at
  `GET /api/integrations/v1/now-playing` exposes a stable, normalized
  shape for third-party music controllers (Music Assistant, custom
  Lovelace cards). External players can show the current track, host
  banter, ads, and station IDs without reverse-engineering the listener
  payload. Includes `ETag` + `Cache-Control` for cheap polling and ten
  sample-payload JSON fixtures committed under
  `docs/integrations/sample-payloads/` as the binding contract.

### Changed

- **The station now defaults to English-first.** New installs render English utility copy on the listener page (with Italian station-feel words intact) and English-first admin, and the AI hosts code-switch with Italian flavor. Turn on **Super Italian Mode** in the add-on options (or the admin Engine Room) for the fully Italian-first experience.

- **The stable add-on now presents as stable in Home Assistant.** The release channel no longer shows the Experimental pill, Edge keeps it, and both add-on folders now include the shared custom AppArmor profile so Supervisor can award the extra security-rating point after install/update.

- **Sports flashes are clearer and less shouty** — Sports news now uses a steadier host selection path, asks for informed radio-desk updates instead of maximum-excitement commentary, and no longer adds a dedicated sports TTS speed/pitch spike.

### Fixed

- **The Admin Token help text now says what the token actually does.** The Admin Token field used to claim it was needed for the Home Assistant media player — it isn't; the media player works whether or not you set it. The description now explains the token covers the admin panel and any automations that call the station directly, so a blank token no longer reads as riskier than it is.

- **The hosts sound right now.** Marco reads clearly instead of mumbling, and Giulia sounds like the 80-year-old Nonna she is written as instead of a thirty-something. Each host's voice can now be dialed in independently in the station config (a per-host `voice_settings`), so tuning one host never disturbs the other.

- **Admin load-more state stays accurate after playlist edits.** The Producer Desk now invalidates cached playlist tails when the rotation changes, hides the load-more button once all loaded rows reach the total, resets load-more buttons after network errors, and skips repeated yt-dlp lookups after web search results are exhausted.

- **Festival Mode no longer leaves ghost tracks in "Up Next".** Switching Festival Mode on now clears the upcoming list at the same instant it clears the queued audio, so the panel always matches what is about to play. Every queue-clearing action now runs through one path, so the list and the audio can't drift apart again.

- **Home Assistant updates now say why they fail, and shrug off a brief hiccup.** When the station can't send its now-playing status to Home Assistant, the add-on log names the real reason instead of an empty line, and the station quietly retries once after a short network blip. Listeners never notice; an operator reading the log finally gets a straight answer.

- **Engine Room track count now reflects the full rotation.** The playlist size stat shows the actual number of tracks in the rotation rather than the most-recently-fetched page size.

- **Loaded playlist pages no longer snap back on refresh.** Tracks added via "Load more" stay visible across status polls instead of collapsing back to the first page on the next cycle.

- **Admin programme durations are now truthful.** Status payloads expose real current segment duration/progress and stream-log durations, and the admin/live/listener UIs no longer invent music, banter, or ad durations when metadata is missing.

### Added

- **Shareable clip moments** — Tap "Condividi clip" on the listener page (or the Clip button on `/live`) to share the last 30 seconds as a branded landing page. The link previews in iMessage and WhatsApp with the station name, the track that was playing, the 30-second audio, and an "Ascolta in diretta" button. Expired and missing clips show a friendly "Questo momento è passato" page instead of a 404.
- **Host interrupt trigger** — When a Home Assistant timer fires, the hosts immediately interrupt whatever is playing and deliver an urgent banter segment telling the listener to act. Configure per-timer directives in `radio.toml` under `[[homeassistant.timer_interrupt]]`. The same mechanism is exposed as `POST /api/interrupt`, so any HA automation (motion sensor, alarm, dishwasher done) can inject a custom directive into the stream without code changes.
- **Admin producer desk** — The admin panel is reorganized around the live broadcast: an On Air zone (current segment, transport controls, running AI cost), a Live Queue holding the forward Scaletta, and a Rotation Pool, with secondary controls tucked into collapsible drawers. Operators can drop a single queued segment without clearing the whole queue.
- **Stream audio format on `/public-status`** — The public payload now exposes a `stream.audio_format` object (codec, mime type, bitrate, sample rate, channels). External integrations can declare `/stream` correctly before playback instead of assuming the default MP3/192k configuration.

### Changed

- **Banter cadence minimum is now 2 songs.** Previously a value of `1` for `songs_between_banter` made the hosts talk after every single song. The admin Cadenza slider and config validation now enforce a floor of 2.

### Fixed

- **Host banter is no longer truncated to its first phrase.** Per-line voice normalization had been trimming silence with a setting that stopped output at the first pause, collapsing multi-line host exchanges to a second or two. Silence trimming now removes trailing silence only.
- **Pacing API rejects malformed payloads.** Non-object bodies and non-integer fields on `PATCH /api/pacing` now return a clear error instead of a 500 or silent coercion. Cadence values are clamped to a safe ceiling so a single request cannot effectively disable banter or ads.
- **yt-dlp downloads now time out after 30 seconds.** A hung YouTube connection can no longer permanently block a thread in the audio pipeline.
- **`httpx` and `httpcore` request logs are quiet by default.** Successful outbound HTTP calls no longer flood the log stream. Set `MAMMAMIRADIO_HTTP_LOG_LEVEL=INFO` (or `DEBUG`) to re-enable detailed traffic logs.

## 2.12.4

### Added

- **Edge add-on (development channel)** — A second add-on, **Mamma Mi Radio (Edge)**, now ships from this repository and tracks the latest development build. Install the stable **Mamma Mi Radio** add-on for daily listening; Edge is for testing. The two share one image and cannot run at the same time (both use port 8000).
- **Runtime status in the Engine Room** — The admin Engine Room now shows a live health indicator and a Runtime Status card: which audio, script, and voice providers are active, plus any recent fallbacks.

### Fixed

- **Spoken host segments are assembled more strictly.** Broken or implausibly short multi-line banter is now rejected before it can reach the listener, instead of playing a malformed segment.
- **Admin control touch targets meet the 44 px accessibility minimum.** Mode toggles and other admin controls are easier to tap on phones and tablets.

## 2.12.3

### Added

- **HA Green performance smoke gate** — `make perf-smoke` now checks a live station's health, readiness, public runtime status, and first stream byte against configurable HA Green thresholds.
- **Festival Mode** — New `festival_mode` add-on option. When enabled, the AI hosts become theatrical music competition MCs: songs are introduced as fictional Italian-regional delegations, dramatic points are assigned, and drinking game triggers are called. Toggleable live from the admin panel without an add-on restart; persisted through `/data/options.json` so it survives restarts.

### Changed

- **Queue fallback starts before the health-failure window.** Active listeners now get cache rescue attempts after a 5-second bounded queue-empty wait, before the preserved 30-second silence health-failure threshold triggers.
- **Italian-first is now the default.** New add-on installs default `super_italian_mode` to `true`, while the option remains available for operators who want the older code-switching style.
- **Jamendo can participate in the normal programme.** When charts and Jamendo are both configured, startup blends Jamendo tracks into the chart rotation instead of keeping Jamendo fallback-only.
- **Admin source chips enrich instead of replacing the programme.** Jamendo, chart reload, and decade buttons add tracks into the current rotation without purging the queue, skipping current playback, or clearing listener requests.

### Fixed

- **Cache rescue no longer repeats the first cached song by filename.** Empty-queue fallback avoids the current/recent song when alternatives exist and randomizes the rescue candidate, so skip is less likely to land back on the same cached track.
- **Palinsesto hides scheduler pool diagnostics and duplicate current rows.** Pool badges/wrap notes no longer appear in the operator programme, and the current segment is filtered out of history.
- **Speech/ad transition stacking is reduced.** Segments that already carry a music-tail crossfade no longer receive an extra transition sting before them.
- **Empty-queue skip is safer on HA Green.** Skip records a bridge action and forces next music before cutting when the queue is empty, and status exposes skip readiness.
- **Ad disclaimer speed is deterministic by format.** The old near-2x role spike is replaced with format-scoped pacing.

## 2.12.2

### Fixed

- **Palinsesto table no longer causes horizontal overflow on phone widths.** The six-column programme table now collapses into compact cards on phone widths and stays inside its panel on desktop.
- **Anthropic usage-limit errors now trip the provider circuit breaker.** Account quota/credit exhaustion suspends Anthropic for the existing cooldown and falls through to OpenAI immediately, instead of retrying Anthropic on every host segment while HA Green waits.

## 2.12.1

### Added

- **Chaos Mode for host banter** — Adds the `chaos_mode_active` add-on option and admin `/api/chaos` persistence path. The toggle survives add-on restarts through `/data/options.json` and can be controlled from the admin Radio tab.

### Changed

- **Listener-request public IDs are split from admin mutation IDs.** The public request feed now exposes `public_token` for listener-side tracking and keeps the admin-only `request_id` out of the public payload.
- **Banter history now separates queued tracks from heard tracks** — `played_track_log` records music when it actually starts streaming, so chaos impossible-recall prompts only reference songs listeners really heard.

### Fixed

- **Provider key checks no longer stack overlapping probes.** Rapid clicks on the setup provider check now share the active result instead of launching duplicate Anthropic/OpenAI probe sets.
- **Listener-request rate limiting respects HA ingress client headers.** Requests through trusted local proxy paths bucket by the real listener IP while direct callers cannot spoof forwarded headers.
- **Listener song-request failures leave clear state.** Search failures and shutdown cancellation now mark the request errored instead of leaving it stuck as "still downloading."

## 2.12.0

### Added

- **Jamendo client ID option** — `jamendo_client_id` is now a first-class add-on option. Set it in the add-on configuration to enable CC-licensed music from Jamendo. Leave empty to disable.
- **Secret-safe provider check endpoint** — admin-only `POST /api/setup/provider-check` actively probes the live Anthropic key, OpenAI chat key, and OpenAI TTS key with tiny requests, returning only configured/ok/status/error-category fields. This helps distinguish "the add-on has a bad key" from "local `.env` has a different key" without exposing secrets.
- **Full imaging architecture** — music-to-voice and voice-to-music boundaries now get short branded transition stings, sweepers pick up motif underlays, and banter/news can sit over ducked talk beds for a more continuous station feel. Enabled by default and configurable through the new `[imaging]` block in the add-on `radio.toml`; FFmpeg-generated stings and beds are used automatically when no bundled imaging assets are present.
- **Super Italian Mode toggle** — new `super_italian_mode` addon option (default `false`). Off: listener UI in English with Italian station-feel words intact (`Stasera in onda`, `Palinsesto`, `Mi`, tricolor); AI hosts code-switch with Italian sprinkles. On: listener UI flips to full Italian; hosts lean fully into Italian idioms and address listeners as `amici miei`. Admin UI stays English regardless. Toggle is also exposed in the admin Engine Room and persists via `/data/options.json` so it survives addon container updates.

### Fixed

- **Anthropic model and audio-FX guardrails**: add-on model choices no longer offer retired/invalid Claude 4.5 dated IDs; `claude_model` now offers the existing Haiku default plus current Sonnet/Opus options. Anthropic 404/model-not-found errors now trip a 10-minute provider backoff and fall through to OpenAI once instead of spamming each generation. Synthetic ad beds/foley now clamp generated ffmpeg filter parameters into valid ranges (`aphaser.delay <= 5`, `tremolo.f >= 0.1`), fixing the previously failing `luxury_spa`, `mysterious`, and `cafe` paths.
- **Admin control room reads as espresso warm-brown again.** v2.11.0 shipped with the admin Engine Room washed out to taupe after PR #298 raised four shared `tokens.css` values to make listener cards visible. Tokens reverted to Pi-baseline; listener cards keep the brighter values via inline overrides on `.mmr-stage`, `.mmr-np-bar`, `.btn-ghost`, `.mmr-schedule`, `.mmr-dedica`, `.mmr-about-card`.

## 2.11.1

### Added

- **Listener-request identity fields** — Each request now carries `request_id`, `status`, and a reserved `evict_after` field. The rate-limit key moved to a hashed form so no raw IP is stored. `GET /public-listener-requests` exposes `request_id` and `status` for upcoming sidebar UIs; dismiss accepts both the legacy timestamp id and the new `request_id`.

### Changed

- **Listener song downloads use a bounded executor** — `search_ytdlp_metadata` runs in a separate 2-thread pool so listener download tasks cannot contend with the producer on Pi hardware.

### Fixed

- Rate-limit dict pruned before queue-cap check to prevent unbounded growth under sustained rejection waves.
- Trackless shoutout dismiss no longer clears unrelated pinned tracks set by a sibling song request.

## 2.11.0

The big one for the addon: Italian-trending music as the default Jamendo source, the listener page reads correctly at rest on every viewport we test on, the admin panel is fully in Italian, and the source tree is reshaped around seven subpackages.

### Added

- **Jamendo `country` and `order` filters — Italian-trending music as the default Jamendo source.** Two new fields in `[playlist]` (`jamendo_country`, `jamendo_order`) plus matching `JAMENDO_COUNTRY` / `JAMENDO_ORDER` env-var overrides, validated at config load. The addon's default radio.toml now ships `country = "ITA"` + `order = "popularity_week"`, so the Jamendo source surfaces Italian-trending tracks instead of any-country pop. Same engine + different `country=` is the foundation for future country-specific radio "skins".
- **`--ai-purple` semantic token** in `tokens.css`: `#A855F7` reserved for AI-generated segments so operators can distinguish AI content from human/music at a glance.
- **Accessibility (WCAG 2.1 AA)**: `<html lang="it">` on `admin.html`; sr-only labels on song-request inputs in `listener.html`; `aria-hidden` on decorative tricolor; `.sr-only` and `:focus-visible` utilities in `base.css`; `aria-pressed` synced to play button.
- **Content-based asset fingerprinting** for `/static/*.css` and `/static/*.js`: visual fixes invalidate stale browser URLs even without an addon-version bump.
- **Docker CI smoke test** in `addon-build.yml`: a 40-second live test runs against the freshly built amd64 image — hits `/healthz`, asserts `status != 'failing'` and `queue_empty_elapsed_s <= 30`. Catches "server starts but can't produce audio" without a Pi runner.

### Changed

- **`mammamiradio/` subpackaged into seven subpackages** (`core`, `audio`, `playlist`, `hosts`, `home`, `scheduling`, `web`). Public addon entrypoint `mammamiradio.main:app` unchanged. **Migration note** for any out-of-tree script that imports modules directly: flat paths like `mammamiradio.config`, `mammamiradio.streamer`, `mammamiradio.playlist`, etc. no longer resolve; rewrite to subpackage paths (`mammamiradio.core.config`, `mammamiradio.web.streamer`, …).
- **Repo root reduced to four top-level files; everything else moved under `docs/`.** Cleaner top-level navigation for operators reading the source.
- **Admin panel fully Italianized**: trigger card titles, quick-action chips, filter pills, preset names, slider axis labels, search placeholder/button, engine room headings, setup subheadings, toast strings, and `ON AIR` → `IN ONDA` are now Italian. Eliminates the mixed-language whiplash that remained after the panel shell was italianized but content strings stayed in English.
- **Service worker switched to network-first** for `/listen`, CSS, JS, and `sw.js` itself. Was cache-first; UI fixes were getting stuck behind stale caches and the only escape was a hard-refresh + version bump. Now visual fixes reach a returning listener on the next request.
- **Design system refresh**: `tokens.css` / `base.css` / `waveform.js` extracted; `admin.html` migrated to canonical base.css components; `listener.html` rewritten to a five-band radio-station composition; `/dashboard` surface deleted, redirects to `/admin`.
- **Ad creative system extracted** into `ad_creative.py` (closes #161).
- **Dashboard inline CSS/JS extracted into `/static/`**: moved dashboard styles and scripts out of the HTML template so static assets can be cached, reviewed, and reused normally.
- **`docs/architecture.md`** updated to describe Jamendo's new country+order filter behavior and the soft-migration path.

### Fixed

- **Build validation now fails when the test suite fails.** The coverage ratchet now hard-fails on any non-zero pytest result, reducing the chance of broken images passing CI.
- **Charts source no longer impersonates local files when the charts API returns empty.** When charts returns zero, the chart loader returns empty too instead of mutating in local MP3s under a `kind="charts"` label. Operator dashboard and persisted source kind now tell the truth.
- **Local `music/` is a real startup source.** When `yt-dlp` is disabled and Jamendo isn't configured but MP3s exist in `music/`, they load as a first-class source instead of falling through to demo assets with a misleading warning.
- **Charts `source_id` numerical drift** (`apple_music_it_top_50` → `apple_music_it_top_100`): the URL fetches up to 100 tracks; the persisted label now matches. Transparent migration on read.
- Updated a stale internal test expectation after the asset path migration.
- **Listener cards visible at rest**: surface tokens lifted hard against the espresso body bg so the Schedule, Dedica, and About cards register as panels at a glance, not page bg with a hairline border.
- **Listener page sections silently hidden on Safari and Chrome**: the fixed-position `body::before` glow overlay could be promoted into a compositor layer that occluded scrolled real-viewport content. Removed the fixed overlay; the glow and grain stay in the normal page background. Anchor scroll margins added so sticky navigation cannot hide a target section after a hash jump.
- **Listener now-playing strip never shows "Session stopped"**: idle state used to leak the internal segment label into title and artist slots and broadcast it to the lock screen / Bluetooth / CarPlay via Media Session metadata. Now renders "In pausa" everywhere, with no artist sub-line.
- **Listener page on Safari < 16.2**: `.status-chip` and `.status-dot` use `color-mix()` for their tinted background; older Safari can't parse it and was rendering with no chip background. Added a literal-rgba fallback line above the `color-mix()` declaration.
- **Service worker `/listen` precache restored**: a freshly-installed PWA can now open `/listen` cold-cache offline.
- **Service worker catch-all branch for same-origin GETs**: brand assets (`logo.svg`, future webfonts, future static images) get network-with-cache-fallback handling instead of silently bypassing the cache.
- **Listener mobile** — header overflowed phone viewport, broke vertical scroll, snapped on `In Onda` tap. The pre-Volare phone breakpoint targeted a class name that PR #235 had renamed; never ported. Three layered fixes: phone-breakpoint nav hide, `100svh` for iOS Safari address-bar collapse, `overscroll-behavior-x: contain` to disable horizontal rubber-band. Form inputs bumped to 16 px so iOS Safari stops auto-zooming on focus.
- **Listener brand wordmark — golden "Mi" accent restored** (regression from the Volare class rename).
- **Mobile tap latency and tap-highlight flash on every interactive control**: `-webkit-tap-highlight-color: transparent` on the universal reset and `touch-action: manipulation` on interactive elements. Removes the iOS Safari grey/blue tap-highlight rectangle and the 300 ms double-tap-to-zoom delay. Pinch-zoom on the page itself preserved.
- **Admin brand wordmark — golden "Mi" accent restored.**
- **Admin form fields** no longer trigger iOS Safari auto-zoom on focus (search box and key fields bumped from 13 px to 16 px).
- **Safari banter and news segments cut off after 6–9 seconds**: Safari honoured the Xing/Info VBR duration header embedded by ffmpeg's loudnorm filter and fired `ended` at the declared duration. Two-layer fix: `‑write_xing 0` added to ffmpeg output args; stream-time stripper hardened to handle "free format" frames.
- **Jamendo source-strict downloads**: Jamendo tracks fetch from `direct_url` only — avoids deterministic failures where yt-dlp treated the Jamendo track ID as a YouTube video ID. Cache keys are source-aware so Jamendo and YouTube tracks with the same slug never collide.
- **Producer wakes immediately on session resume**: 1-second `asyncio.sleep` poll replaced with `asyncio.wait_for(resume_event.wait(), timeout=1.0)`. Resume lag drops from worst-case 1s to milliseconds.
- **Silence fallback never queues a silent track**: audio quality circuit breaker recycles the last-known-good music file or drops the segment rather than letting silent audio reach the queue.
- **LRU cache eviction respects the playback queue**: currently-queued norm paths are never deleted mid-stream.
- **LLM prompt injection hardening**: `_sanitize_prompt_data` strips six quote variants and fake role markers (`System:`, `Assistant:`, `Human:`, `User:`, case-insensitive).
- **ICY header injection guard**: station name and genre are CRLF-scrubbed before writing to ICY response headers.
- **`youtube_id` format validation**: `/api/playlist/add-external` validates against `[A-Za-z0-9_-]{11}` before passing to yt-dlp.
- **HA addon version sync**: `ha-addon/mammamiradio/config.yaml` version kept in sync with `pyproject.toml`.
- **Listener brand cleanup**: removed `Napoli` from the hero eyebrow and about-section note. The station fiction is "from Windor to Vergen" via `[sonic_brand].geography`; `Napoli` was leftover seed config.
- **Browser tab title** shortened to just the station name (frequency and city remain in `og:description` for share previews).
- **Host stat typography**: the "I conduttori" stat scaled down so it reads as a labeled stat, not a hero number.
- **Shellcheck warnings resolved** in `ha-addon/mammamiradio/rootfs/run.sh` and `scripts/validate-addon.sh`.

### Removed

- **`/regia` route + `regia.html` template** — the Regia design language already shipped on `/admin` (admin panel title is "Mamma Mi Radio — Regia"); the standalone `/regia` URL served an obsolete prototype duplicate and is gone. Operators land on `/admin` for the control room.
- **Dead `[sonic_brand]` config keys** `short_sting` and `sweeper_probability` — never read by production code. Older operator `radio.toml` files carrying the legacy keys still load cleanly (graceful `pop()`).
- **Dead onboarding/taste-crate copy** in `mammamiradio/playlist/track_rationale.py` and dead taste-mirror helpers in `mammamiradio/hosts/context_cues.py`.
- **567 lines of dead pre-Volare CSS from `listener.css`** — selectors confirmed to have zero matches in the rendered HTML before removal.
- **Dead `probe` parameter from `build_setup_status`** — Spotify-era keyword argument never read or passed.

### Dependencies

- `openai` 2.32.0 → 2.36.0 (script generation; includes `prompt_cache_retention` enum value fix).
- `pydantic-settings` 2.13.1 → 2.14.1.
- Routine: `certifi` 2026.2.25 → 2026.4.22, `click` 8.3.2 → 8.3.3, `idna` 3.11 → 3.13.

## 2.10.10

Brand engine, listener redesign, mobile host control room, and security hardening.

### Added

- **Brand engine (`[brand]` block in `radio.toml`)**: per-station identity layer (name, frequency, city, hosts, theme tokens — colors and curated fonts) separated from operator engine config. Theme overrides Volare Refined defaults with contrast and font-allowlist guards; bad brand config never blocks station boot.
- **Public listener API** (`/public-status` + `/public-listener-requests`): listener page works on any deploy without 401 risk. `listener.js` no longer polls admin-gated `/status`.
- **OpenGraph social cards** (`/og-card.png`) rendered via Pillow with brand colors, station identity, and current track. Falls back to logo SVG on render failure.
- **Listener template migrated to Jinja2** with capability-conditional rendering: PWA, HA, and AI copy toggle based on `[data-cap=KEY]` attributes reading actual capability flags. PWA install replaced with proper `beforeinstallprompt` flow.
- **`/live` mobile host control room** (admin-gated): phone-optimised operator surface for skip / clip / stop / resume.
- **Accessibility (WCAG 2.1 AA)**: `<html lang="it">` on admin; sr-only labels on song-request inputs; aria-hidden on decorative tricolor; focus-visible utilities; aria-pressed sync on play button.
- **Regression test suite** (`tests/test_qa_regression_guards.py`): 14 automated guards covering LRU eviction protection, prompt sanitization, ICY header injection, youtube_id regex, addon version sync, resume_event presence, and the three-tier last-music-file fallback chain.
- **`--ai-purple` semantic token** for AI-generated segments (used in Regia banter cards and peek-panel type dots).
- **Song-to-host "exclaim" transition style**: hosts open with a short Italian musical exclamation — *Bravo!*, *Magnifico!*, *Che canzone!* — before pivoting to speech (10% probability when song cues are present).

### Fixed

- **Listener tricolor + radio cabinet rendered transparent**: a CSS refactor referenced color tokens (`--flag-green`, `--flag-red`, `--flag-white`, `--terracotta`, `--sage`, `--ink`) that were never declared, so Italian flag elements and the vintage radio illustration silently rendered with `rgba(0,0,0,0)`. Tokens now declared in `tokens.css` with warm copper-brown cabinet (`#6B3E2D`) and tan highlights (`#B47850`); a new test guards every `var(--*)` reference resolves to a defined token.
- **Programme Dur. column always empty**: `<td class="du"></td>` rendered blank for every row. New `fmtDur(item, typeKey)` helper reads `duration_ms` (top-level or under metadata) with sensible per-type fallbacks (music 4:00, banter 0:30, ad 1:00, news 0:20).
- **News flash auto-fires reliably**: removed the `random.random() < 0.3` gate; news now fires deterministically once `songs_since_news >= 6` (over hour-long sessions, the random gate sometimes never fired).
- **Listener cards visible at rest**: bumped `.mmr-about-card` from `--surface` to `--surface-strong`; the four About cards now register against the page bg.
- **Regia progress bar always showed 0%**: `Segment.duration_sec` was never populated in `producer.py`. Now probed via `_ffprobe_duration_sec` at the prewarm path and main convergence point.
- **Listener now-playing strip falls through "0h 0m"**: now reads `status.uptime_sec` from `/public-status` (station-wide on-air time) and shows "In diretta" for the first minute.
- **Admin mobile layout — panel header overlap**: title and subtitle stacked vertically below 768px so they don't collide.
- **Local setup now avoids broken Python 3.13 installs**: `conductor-setup.sh` prefers `python3.11 → 3.12 → 3.13 → python3` instead of leading with 3.13.

### Refactored

- **Dashboard inline CSS/JS extracted into `/static/`**: moved dashboard styles and scripts out of the HTML template so static assets can be cached, reviewed, and reused normally.

## 2.10.9

Fixes the admin panel regression introduced in v2.10.8 and adds producer bridge metadata improvements.

### Fixed

- **Admin panel broken by v2.10.8 CSP regression**: `script-src 'self'` blocked the entire inline script block in `admin.html`, and a `nonce`-based intermediate attempt blocked the ~40 inline event handlers. Final fix: `script-src 'self' 'unsafe-inline'`, which allows all inline code while still blocking external script sources. The `esc()` wrappers from 2.10.8 remain the load-bearing XSS defense.
- **Producer bridge track metadata**: Resume bridge and idle bridge segments now call `load_track_metadata()` before humanizing the filename, so `title` and `artist` are populated from the sidecar JSON when available instead of falling back to raw filename stems.

### Security

- CSP on `/admin` now uses `script-src 'self' 'unsafe-inline'`, blocking external script injection (the operationally relevant threat) while allowing the inline code the admin panel depends on.


## 2.10.8

Security fix: stored XSS in admin panel Engine Room via HA entity state injection and yt-dlp track title injection.

### Security

- **Stored XSS via HA entity state values**: Five Home Assistant-sourced fields (`mood`, `weather_arc`, `events_summary`, `pending_directive`, `last_event_label`) were rendered via `innerHTML` without escaping. All five are now wrapped with `esc()` before assignment.
- **Stored XSS via yt-dlp track titles**: Maliciously named YouTube videos could inject HTML/JS via `ha_pending_directive`. Same `esc()` wrapper in `admin.html` covers this field. Raw storage is preserved for LLM prompt quality; HTML encoding only happens at the render site.
- **Content-Security-Policy on `/admin`**: The `/admin` route now sets a `Content-Security-Policy` header as defense-in-depth.


## 2.10.7

Reliability and UI-truth fixes across playback fallback, AI fallback, queue labeling, and content hygiene.

### Fixed

- Anthropic auth flood no longer fires under concurrent load: attempt lock serializes the 401 cooldown check across sibling banter/ad/transition calls. First 401 trips the 10-minute backoff; concurrent callers see the block and use the OpenAI fallback.
- TTS voice validation at config load: invalid voice IDs (e.g. `onyx` on an edge-tts host, typos in edge voice IDs) are now logged once and replaced with `it-IT-DiegoNeural` before any synthesis attempt. Runtime TTS failures are memoized per-session so a flaky voice doesn't re-attempt per segment. `/api/capabilities` gains a `tts_degraded` flag when any voice was substituted.
- Queue starvation rescue: when the queue is empty for 30s and no canned clip or norm-cache is available, playback falls back to a random MP3 from `demo_assets/music/` instead of looping silently. Bundled demo tracks in `demo_assets/music/` (named `Artist - Title.mp3`) are preferred over placeholder tones at startup and when demo source is explicitly selected.
- `/readyz` honors stopped state: a stopped session now returns `503 stopped` even when the queue is populated, so HA Supervisor no longer routes listeners to a deliberately paused station.
- Chart ingest filters non-music entries: Apple Music's Italian chart occasionally surfaces podcasts, BBC comedy, and audiobooks that played as dead-eye audio and broke the radio illusion. Narrow content filter drops obvious non-music before it reaches the queue.
- Rejected downloads purge + denylist: `validate_download` failures now purge the cache file and add the cache key to a per-session denylist. Producer, prefetch, and prewarm short-circuit on denylisted keys so the same broken track cannot loop forever.
- Queue rows no longer render bare segment types for BANTER, AD, STATION ID, SWEEPER, or TIME CHECK. BANTER rows now show the participating hosts (`Marco & Luca`), canned clips show `Pre-recorded banter`, AD breaks show `Ad: Barella Pasta +2 more`, station IDs show `Station ID`, sweepers show `Station sweeper`, and time checks show the spoken time. News-flash and error-recovery segments pick up their own labels. Admin queue render also hardened to hide a label that equals the bare type, so a future producer path that forgets to set a title can't re-introduce the `BANTER banter` row.
- Dashboard "AI" pipeline pill no longer lies when Anthropic is auth-suspended. Dashboard now mirrors the three-state logic `admin.html` already had: a configured-but-suspended Anthropic shows `AI Fallback` instead of `AI`.


## 2.10.6

UI truth and playback safety fixes, plus a normalizer concat duration guard.

### Fixed

- Normalizer `concat_files` now probes input durations with `ffprobe` and logs a WARNING when the concatenated output is shorter than expected. Fail-open when ffprobe is unavailable.
- Stopped state actually stops: Stop freezes dashboard animations, pauses the elapsed-time counter, and disables producer buttons.
- Admin panel distinguishes *connected*, *not configured*, and *suspended* Anthropic states instead of flashing "connected" while 401s are failing every call.
- Scheduler reason strings (`cooldown: 45s`, `banter_due_in=3`) no longer leak to listener-facing up-next rows.
- Norm-cache rescue path no longer shows raw filenames as titles. Sidecar metadata used when present; otherwise humanized (`norm_busted.mp3` → `Busted`).


## 2.10.5

### Changed

- Admin UI redesign: two-column control room layout with warm sidebar, compact now-playing card, waveform/progress, 2×2 quick-controls grid (Next / Pause / Shuffle / Banter), unified "On Air" programme list with NOW badge, and filter pills (All / Music / Banter / Ads). Pacing, Hosts, Station Log, Engine Room collapsed into accordions.
- Token cost counter regression fix: static element no longer shadows the dynamic Engine Room cost display.
- Stop/Resume 2×2 grid fix: no more visual gap when toggling Stop↔Resume.
- Accessibility polish: keyboard `:focus-visible` ring on buttons/inputs, 44px touch-target floor on controls (36px chips, 32px pills), base font-size raised to 16px (WCAG), queue title raised to 14px, HA slider labels raised to 9px/32% opacity.
- Quick Action labels renamed to action-oriented verbs (trim / force).
- Dead `btn-skip` CSS removed; hardcoded hover hex replaced with `color-mix` on the accent token.


## 2.10.4

### Security

- CI action SHA-pinned: `dependabot/fetch-metadata` now pinned to commit SHA (supply chain hardening).
- Added `.gitleaks.toml` for secret scanning (Anthropic API keys, HA tokens).
- Raised `yt-dlp` minimum version to `>=2026.2.21` (patches GHSA-g3gw-q23r-pgqm).


## 2.10.3

### Added
- `POST /api/hot-reload`: reloads `mammamiradio.scriptwriter` in place without interrupting the stream.
- Quick Actions chips in admin UI: one-tap controls for Less banter / More chaos / Too many ads / Hot reload.

### Changed
- Producer now imports `mammamiradio.scriptwriter` as a module reference so hot reload applies at every call site.
- `_has_script_llm` renamed to `has_script_llm` for the new module-reference import pattern.
- HA add-on `radio.toml` now ships byte-for-byte identical to the root `radio.toml`. The Pi-specific pacing overrides (`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`) are removed; CI, the local validator, and `tests/test_addon_radio_sync.py` all enforce strict `cmp -s`.
- Broadcast EQ restored to the 3-filter chain.
- Auto-resume on listener connect removed. A deliberate `/api/stop` stays paused across restarts until explicit `/api/resume`.


## 2.10.2

### Fixed
- **Critical**: Silence after HA restart following a deliberate stop. The `session_stopped.flag` survived restarts — any listener connecting after a restart heard nothing until a manual admin resume. Fixed: listener connecting now auto-clears the stopped state.
- **Critical**: 55-75 second silence on resume and idle wakeup on Pi. No canned banter clips ship in the container, so the bridge had no audio to play. Both resume and idle bridges now fall back to the first pre-normalized track in cache, available immediately without FFmpeg.
- **Critical**: FFmpeg 8.1 SIGABRT during normalization on Pi aarch64. Three equalizer filters + loudnorm trigger a `calc_energy` assertion crash (`psymodel.c:576`). Third equalizer removed. Every track was silently failing to normalize, leaving the queue permanently empty.
- Stream player no longer requires a page reload after admin resume. Auto-reconnects within 300ms of the status flip.


## 2.10.1

### Fixed
- **Critical**: Docker images for 2.10.0 were never built. The CI validate job used a strict byte-comparison of `radio.toml` files, but the HA add-on intentionally carries Pi/HA Green pacing overrides. Validate always failed, blocking image builds — HA Supervisor got `[404] manifest unknown` on every update attempt.
- **Pi pacing tuning discarded**: The build step was copying the root `radio.toml` (higher CPU load defaults) over the HA-specific one, shipping the wrong pacing values baked into the image.
