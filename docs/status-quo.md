# Mamma Mi Radio status quo

> Snapshot: 19 August 2026, based on remote `main` at commit
> [`7e6deecd`](https://github.com/florianhorner/mammamiradio/commit/7e6deecdfb3723a32efbb9e7d53cd40c7942b5f5).
> This is a dated product and market assessment, not a live health
> page. This assessment did not audit the running Home Assistant Green
> installation.

As of this snapshot, Mamma Mi Radio is a released, voice-native Home Assistant
product with strong engineering evidence and one useful household observation.
Its Music Assistant provider is merged into the 2.10 pre-release catalog and
offers a path to wider distribution once a stable Music Assistant release
includes it. Market evidence remains thin.

## Status quo map

| Area | Status today | Evidence level |
| --- | --- | --- |
| Core product | A self-hosted, Home Assistant-native radio station for real household speakers | Released |
| User problem | Smart-home owners build sophisticated systems whose value remains invisible to partners, family, and guests | Clear founder insight; market size untested |
| Product promise | Turn automation into atmosphere: the home becomes part of a shared radio experience | Working product thesis |
| Experience | Music, recurring hosts, fictional ads and news, station imaging, and consented home moments in one continuous stream | Shipped |
| Strongest "aha" | Seven dinner guests accepted it as radio until the hosts called out the ignored pasta timer and someone stopped to ask what the radio had said | Founder-observed qualitative evidence |
| Delivery | Home Assistant OS app, optional HACS companion, Docker/local Python, listener UI, control room, MP3 stream, and integration APIs | Shipped |
| AI and voice | Anthropic/OpenAI scriptwriting; Edge, OpenAI, Azure, and ElevenLabs voice routing; distinct host and character voices | Shipped |
| Locality | The application, configuration, keys, state, mixing, and stream run on the user's hardware | Shipped |
| Cloud boundary | Dynamic writing and the best voices still depend on external providers. Edge TTS is also an online service. Mamma Mi Radio is self-hosted and needs internet access for the full experience | Current limitation |
| Privacy | Filtered context preview, entity muting, narrow defaults for new installations, opt-in sensitive moments, and no home-context transmission to a script model without a provider key | Shipped |
| Reliability | Ahead-of-playback production, continuity reservations, cached recovery, emergency audio, bounded retries, provider circuit breakers, listener-delivery receipts, and fail-closed speech when every TTS route is down | Strong CI evidence; in the public v2.18.0 release |
| Candidate reliability fix | An empty music crate no longer loops the station ident. Current `main` also keeps First Listen honest when only recovery audio is available | On `main`; not yet cut |
| Listener truth | The system distinguishes generated, queued, and heard content. v2.18.0 shipped anonymous aggregate listening epochs and one bounded companionship moment after sustained listening. Current `main` adds now-playing that names only the hosts who spoke, and runtime ad session receipts | v2.18.0 public; later items not yet cut |
| Stable release | v2.18.0, published 8 August 2026 | Public |
| Current development | Post-2.18.0 work sits in `## [Unreleased]`; `main` advertises the published v2.18.0 so the app stays installable; latest `main` CI is green | Not yet cut |
| Current distribution | Custom Home Assistant app repository, HACS companion, Docker, and direct installation | Available but founder-led |
| Next distribution step | Music Assistant provider merged 11 August 2026. It is an alpha catalog entry on the 2.10.0 pre-release channel. It is not in any stable Music Assistant release | Pre-release only; not yet stable |
| Potential reach | A stable Music Assistant release that includes it would make it discoverable in Music Assistant's built-in provider catalog. Opt-in analytics show about 53,000 active Home Assistant installations reporting Music Assistant | Potential distribution, not adoption |
| Demand evidence | Founder use, informal interest from colleagues, and the seven-guest dinner reaction | Anecdotal |
| Missing evidence | External household retention, repeat use, willingness to configure provider keys, willingness to pay, and a repeatable self-serve installation funnel | Unproven |
| Public traction | Four GitHub stars, two forks, and no replies on the first-listen feedback discussion | Minimal |
| Business model | Users supply their hardware, music access, and provider keys. No demonstrated pricing, revenue, or hosted-service model | Unvalidated |
| Disclosure boundary | Product thesis and public proof can be shared. Household data, pilot identities, outreach notes, and raw interviews stay private | Defined operating rule |
| Long-term direction | Open provider choice and more local AI, aligned with Home Assistant's philosophy | Strategic direction, not today's product |

## Mamma Mi Radio today

Mamma Mi Radio is a self-hosted radio station built for Home Assistant. It
combines music, recurring presenters, fictional advertising and news, station
imaging, and authorized events from the home into a continuous, personalized
broadcast on real household speakers.

Spotify and Apple Music play music; Home Assistant controls devices. Mamma Mi
Radio connects those functions so a sophisticated smart home makes sense to
people who have no interest in dashboards, sensors, or automation logic.

The target user is the Home Assistant enthusiast who has invested thousands in
sensors, actuators, automations, and dashboards, then hears a partner or friend
ask: "Why is that better than a light switch?" Mamma Mi Radio turns the
invisible system into atmosphere. The station can weave a timer or an arrival
into something the whole room hears.

The clearest evidence came over dinner with seven guests. The Italian hosts
called out the founder for ignoring the pasta timer. One guest stopped the
conversation and asked: "Wait, what did the radio just say? Did you hear that?"
The radio had made an invisible automation legible to everyone at the table.

The dinner gives one qualitative data point. It shows that the concept can
create surprise and social recognition. It says nothing yet about whether
outside households will install the product, tolerate the provider setup, keep
listening after the novelty fades, or pay for it.

The engineering is much further along than the market evidence. One FastAPI
application runs the asynchronous producer, playback queue, streaming fan-out,
local state, and FFmpeg audio pipeline. Music provides the spine. Probabilistic
systems write and voice the material around it.

The code puts deterministic boundaries around probabilistic models. Generated
scripts must satisfy structured contracts, language rules, character rules,
length limits, and listener-safety checks.
Provider failures hit bounded retries and circuit breakers. Slow generation
cannot stop the music. The station writes durable memory after the material
reaches a listener without a delivery failure. Public and operator surfaces
distinguish generated, queued, and received content. If every configured voice
route fails, required speech falls through to canned copy or music rather than
a silent file.

Voice carries the product identity. Hosts and advertising characters have
distinct voices, delivery settings, and fallback identities. ElevenLabs
Multilingual v2 supplies the most expressive character voices; the audition
workflow tests stability and style in broadcast context. The station accounts
for paid speech, keeps credentials on the user's installation, memoizes failed
cloud voices, and stops repeated calls to a provider after failure.

The full experience needs cloud providers. Dynamic host writing requires
Anthropic or OpenAI, while the best voices use ElevenLabs, Azure, or OpenAI. The
application is self-hosted and has no Mamma Mi Radio account, central server, or
product telemetry. Mamma Mi Radio is self-hosted and cloud-assisted. Without an
AI key, the station boots with stock host copy and fallback voices, which proves
the signal path but leaves out the dynamic-host experience.

The station treats home context as a trust boundary. Users can preview the
filtered information available to the hosts, mute individual entities, disable
host context while retaining the integration, and avoid sending home context
to a script model by running without a script-provider key. New installations
start with a narrow context set. Sensitive presence and household moments
require explicit enablement. The privacy target is the smallest amount of
context that creates recognition without becoming creepy.

The current remote `main` passes 7,300 tests at 92.40% coverage. Its ARM smoke
test reaches the first stream byte in 0.01 seconds. Since v2.18.0, `main` has
also taken a First Listen path from setup to real radio, copyright-safe starter
music with on-air attribution, the Modern Night Drive imaging pack, runtime ad
session receipts, now-playing that names only who spoke, and a fix that stops
an empty crate from looping the recovery ident. These numbers describe
engineering behavior. They do not measure household demand.

Version 2.18.0 remains the stable release. The unpublished work on `main` does
not yet have a version number. It adds a guided First Listen, a rights-aware
starter catalog that is not yet in a published package, recorded station
imaging, more honest now-playing and ad receipts, and the empty-crate recovery
fix. Current CI and repository state do not establish present device uptime.

Distribution is the next engineering milestone. The upstream Music Assistant
provider was merged on 11 August 2026. It already uses a versioned now-playing
contract with typed music, voice, and interstitial segments, host attribution,
audio-format discovery, and conditional metadata polling. Beta and
release-candidate Music Assistant 2.10.0 users can add it as an alpha
provider; stable 2.9.x users cannot, since it is not in a stable Music
Assistant release.

A stable Music Assistant release that includes the provider would improve
discoverability and reduce integration friction. Household adoption remains a
separate test.

The product has answered much of its engineering question: it can behave like a
radio station through provider failures and thin queues. v2.18.0 closed the
all-routes TTS silence path in a public release. Current `main` also stops an
empty crate from looping the recovery ident, but that fix is not yet cut. It
has not answered the market question. Five outside households now need to show
whether people want to keep it in the room.

## Public evidence

- [GitHub repository](https://github.com/florianhorner/mammamiradio)
- [v2.18.0 release](https://github.com/florianhorner/mammamiradio/releases/tag/v2.18.0)
- [Quality run: 7,300 passing tests and 92.40% coverage](https://github.com/florianhorner/mammamiradio/actions/runs/32087219116)
- [ARM smoke: first stream byte in 0.01 seconds](https://github.com/florianhorner/mammamiradio/actions/runs/32087219082)
- [Music Assistant provider, merged 11 August 2026](https://github.com/music-assistant/server/pull/3836)
- [First-listen feedback discussion](https://github.com/florianhorner/mammamiradio/discussions/831)
- [Music Assistant integration](https://www.home-assistant.io/integrations/music_assistant/)
- [Home Assistant analytics](https://analytics.home-assistant.io/)
