<p align="center">
  <img src="mammamiradio/logo.png" width="128" height="128" alt="Mamma Mi Radio logo">
</p>

# Home Assistant Add-ons: Mamma Mi Radio

Add-on repository for [mammamiradio](https://github.com/florianhorner/mammamiradio), an AI-powered Italian radio station.

## Installation

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the three dots menu (top right) > **Repositories**
3. Paste this URL: `https://github.com/florianhorner/mammamiradio`
4. Click **Add**, then find "Mamma Mi Radio" in the store
5. Click **Install**

## Configuration

After installing, go to the add-on's **Configuration** tab:

- **Anthropic API Key** (optional): Enables Claude-generated banter and ads. Get one at [console.anthropic.com](https://console.anthropic.com). Without this, the station uses stock banter lines.
- **OpenAI API Key** (optional): Enables OpenAI `gpt-4o-mini-tts` host voices and serves as a script generation fallback when Anthropic is unavailable.
- **Station Name**: Customize your station's name (default: "Mamma Mi Radio").

## Usage

1. Start the add-on
2. Open it from the HA sidebar / ingress entry first. The mapped `:8000` port is mainly for `/stream`, `/healthz`, and direct diagnostics
3. The dashboard shows your station's current tier (Demo Radio, Full AI Radio, or Connected Home) and a guide for what to set up next
4. Add an Anthropic API key to unlock live AI hosts
5. Use the add-on's `/stream` endpoint with HA media players

The add-on also exposes unauthenticated `/healthz` and `/readyz` probes for monitoring. The richer setup checks live behind the admin UI at `/api/setup/status`, `/api/setup/recheck`, and `/api/setup/addon-snippet`.

### Playing on speakers

To play the radio on a smart speaker or media player in Home Assistant, use the `media_player.play_media` service:

```yaml
service: media_player.play_media
target:
  entity_id: media_player.your_speaker
data:
  media_content_id: http://[YOUR_HA_IP]:8000/stream
  media_content_type: music
```

Or add a button to your Lovelace dashboard that triggers this automation.

## Screenshots

The dashboard gives you the station at a glance — now playing, up-next queue, controls, and setup prompts:

![Dashboard](../docs/screenshots/dashboard.png)

The listener page is a clean, mobile-friendly player for anyone on your network:

![Listener](../docs/screenshots/listener.png)

## What it does

- Streams a continuous AI-generated Italian radio station
- Hosts reference your actual Home Assistant state (lights, temperature, who's home)
- Remembers returning listeners across sessions with compounding persona memory
- Rotates between music, host banter, and absurd fake Italian ads
- Falls back gracefully when optional services are unavailable
