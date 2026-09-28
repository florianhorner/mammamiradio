# Mamma Mi Radio

<!-- shared-listing-body: byte-identical across Stable and Edge, enforced by scripts/validate-addon.sh -->

![The Mamma Mi Radio listener page](https://raw.githubusercontent.com/florianhorner/mammamiradio/main/docs/screenshots/listener.png)

Marco and Giulia run a late-night Italian radio show, mostly in English, on
your own Home Assistant. They play music, argue like two hosts who have known
each other too long, and read ads for forty companies that do not exist. The
station plays before you configure anything: open it, select **Start my
station**, and the show is on.

## What plays with no key at all

- Twelve credited starter tracks, about 47 minutes, that play offline
- 21 prerecorded breaks from Marco and Giulia, about 26 minutes, with no
  repeats until the whole set has aired
- 11 finished fictional ads, seven in English and four in Italian for Super
  Italian mode
- The Modern Night Drive station sound, 47 finished pieces
- Your own music, from a folder in the Home Assistant Media panel

The packaged audio makes no provider call. Other stock lines can use Edge TTS,
which needs no key but does go online.

## What an AI key adds

An Anthropic or OpenAI key gives the hosts banter, news flashes and ad breaks
written on the fly, and lets them react when you steer the music in your own
words. Usage is billed to your own account, and the control room shows a
running estimate split by category. ElevenLabs credentials give Marco and
Giulia the cast voices from the public demos; OpenAI or Azure Speech offer
alternative voices.

## Your home on the air

On a new install the house stays off the air until you preview what the hosts
would get and say yes. Today that is daylight and the weather. The laundry and
arrival moments in the demo come from broader sharing that a later update
adds.

It is not a voice assistant and does not run household automations. No
account, no central service, no subscription.

## Playing it on your speakers

The stream plays in the browser on the device you use, and through whatever
that device outputs to. The optional
[Mamma Mi Radio integration](https://github.com/florianhorner/mammamiradio/blob/main/docs/integrations/ha-integration.md#install-the-hacs-integration-for-ha-native-playback)
from HACS adds the station as a media source for Home Assistant speakers.

## Everything else

The Documentation tab of the **Mamma Mi Radio** app covers configuration, the
guided first run, speaker setup, and what to do when something sounds wrong.
It is also online as
[the add-on documentation](https://github.com/florianhorner/mammamiradio/blob/main/ha-addon/mammamiradio/DOCS.md).
