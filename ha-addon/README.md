# Home Assistant Add-ons: Mamma Mi Radio

Add-on repository for [mammamiradio](https://github.com/florianhorner/fakeitaliradio), an AI-powered Italian radio station.

## Installation

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the three dots menu (top right) > **Repositories**
3. Paste this URL: `https://github.com/florianhorner/fakeitaliradio`
4. Click **Add**, then find "Mamma Mi Radio" in the store
5. Click **Install**

## Configuration

After installing, go to the add-on's **Configuration** tab:

- **Anthropic API Key** (recommended): Enables AI-generated banter and ads. Get one at [console.anthropic.com](https://console.anthropic.com). Without this, the station uses stock banter lines.
- **Spotify Client ID / Secret** (optional): From [developer.spotify.com](https://developer.spotify.com/dashboard). Without these, the station uses a built-in demo Italian playlist.
- **Station Name**: Customize your station's name (default: "Radio Italì").
- **Spotify Playlist URL** (optional): A specific Spotify playlist to use.

## Usage

1. Start the add-on
2. Click **Open Web UI** in the sidebar (or the add-on info page) to access the dashboard
3. The stream is available at the add-on's `/stream` endpoint for use with HA media players

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

## What it does

- Streams a continuous AI-generated Italian radio station
- Hosts reference your actual Home Assistant state (lights, temperature, who's home)
- Rotates between music, host banter, and absurd fake Italian ads
- Falls back gracefully when optional services are unavailable
