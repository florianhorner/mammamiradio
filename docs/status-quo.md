# Mamma Mi Radio status quo

> Snapshot: 15 July 2026, based on remote `main` at commit
> [`93121160`](https://github.com/florianhorner/mammamiradio/commit/93121160e43fc36a26bea210cf840251bb2d98e8).
> This is a dated product and market assessment, not a live health
> page. This assessment did not audit the running Home Assistant Green
> installation.

As of this snapshot, Mamma Mi Radio is a released, voice-native Home Assistant
product with strong engineering evidence and one useful household observation.
Its upstream Music Assistant submission offers a path to wider distribution.
Market evidence remains thin.

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
| Reliability | Ahead-of-playback production, continuity reservations, cached recovery, emergency audio, bounded retries, provider circuit breakers, and listener-delivery receipts | Strong CI evidence |
| Candidate reliability fix | The current v2.18 candidate fails closed when every configured TTS route is unavailable: required speech enters canned or continuity recovery instead of becoming a silent speech file | Implemented locally; pending merge, public CI, release, and runtime soak |
| Listener truth | The system distinguishes generated, queued, and heard content. Current `main` adds anonymous aggregate listening epochs and one bounded companionship moment after sustained listening | In v2.18 candidate |
| Stable release | v2.17.0, published 12 July 2026 | Public |
| Current development | v2.18.0 rolling candidate; latest `main` CI is green | Not yet the stable release |
| Current distribution | Custom Home Assistant app repository, HACS companion, Docker, and direct installation | Available but founder-led |
| Next distribution step | Upstream Music Assistant provider submission with typed now-playing metadata | Open, checks green, changes requested; not merged |
| Potential reach | If accepted, it becomes discoverable in Music Assistant's built-in provider catalog. Opt-in analytics show about 64,000 active Home Assistant installations reporting Music Assistant | Potential distribution, not adoption |
| Demand evidence | Founder use, informal interest from colleagues, and the seven-guest dinner reaction | Anecdotal |
| Missing evidence | External household retention, repeat use, willingness to configure provider keys, willingness to pay, and a repeatable self-serve installation funnel | Unproven |
| Public traction | Four GitHub stars, two forks, and no replies yet on the first-listen feedback discussion | Minimal |
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
distinguish generated, queued, and received content.

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

The current remote `main` passes 5,199 tests at 92.44% coverage. Its ARM smoke
test reaches the first stream byte in 0.81 seconds. The latest work also adds
truthful aggregate listener sessions: short connection gaps remain part of one
anonymous station epoch, and a sustained listening period can earn one
companionship beat without pretending a specific person arrived or returned.
These numbers describe engineering behavior. They do not measure household
demand.

Version 2.17.0 remains the stable release. The repository is preparing v2.18.0,
which adds more honest listener-session semantics, better continuity
diagnostics, narrower fresh-install context, improved language enforcement,
and stronger protection against stale or failed audio returning later. Current
CI and repository state do not establish present device uptime.

Distribution is the next engineering milestone. An upstream Music Assistant
submission would make Mamma Mi Radio a built-in provider alongside services
such as ORF Radiothek, BBC Sounds, and Radio Paradise. It already uses a
versioned now-playing contract with typed music, voice, and interstitial
segments, host attribution, audio-format discovery, and conditional metadata
polling. The submission is open and its checks pass, but maintainers have
requested changes. It is not merged or released.

A Music Assistant merge would improve discoverability and reduce integration
friction. Household adoption remains a separate test.

The product has answered much of its engineering question: it can behave like a
radio station through provider failures and thin queues. The current v2.18
candidate closes the all-routes TTS silence path in code, but it is not yet
merged, publicly CI-verified, released, or runtime-soaked. It has not answered
the market question. Five outside households now need to show whether people
want to keep it in the room.

## Public evidence

- [GitHub repository](https://github.com/florianhorner/mammamiradio)
- [v2.17.0 release](https://github.com/florianhorner/mammamiradio/releases/tag/v2.17.0)
- [Quality run: 5,199 passing tests and 92.44% coverage](https://github.com/florianhorner/mammamiradio/actions/runs/29401501565)
- [ARM smoke: first stream byte in 0.81 seconds](https://github.com/florianhorner/mammamiradio/actions/runs/29401501465)
- [Music Assistant provider submission](https://github.com/music-assistant/server/pull/3836)
- [First-listen feedback discussion](https://github.com/florianhorner/mammamiradio/discussions/831)
- [Music Assistant integration](https://www.home-assistant.io/integrations/music_assistant/)
- [Home Assistant analytics](https://analytics.home-assistant.io/)
