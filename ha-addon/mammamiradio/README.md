<!-- shared-listing-body: byte-identical across Stable and Edge, enforced by scripts/validate-addon.sh -->

Marco and Giulia run a late-night Italian radio show, mostly in English, on
your own Home Assistant. They play music, argue like two hosts who have known
each other too long, and read ads for forty companies that do not exist. Open
it, select **Start my station**, and the show is on before you configure
anything.

![The Mamma Mi Radio listener page](https://raw.githubusercontent.com/florianhorner/mammamiradio/main/docs/screenshots/listener.png)

**With no key at all:** twelve credited starter tracks (about 47 minutes,
offline), 21 prerecorded breaks from Marco and Giulia, 11 finished fictional
ads, the Modern Night Drive station sound, and your own music from a folder in
the Home Assistant Media panel. Packaged audio makes no provider call; other
stock lines can use Edge TTS, which needs no key but does go online.

**With an AI key:** Anthropic or OpenAI writes banter, news flashes and ad
breaks on the fly, billed to your own account, with a running estimate in the
control room. ElevenLabs gives Marco and Giulia the cast voices from the public
demos; OpenAI or Azure Speech offer alternatives.

**Your home:** on a new install the house stays off the air until you preview
what the hosts would get and say yes. Today that is daylight and the weather.
The laundry and arrival moments in the demo come from broader sharing that a
later update adds. It is not a voice assistant and does not run household
automations. No account, no central service, no subscription.

**Speakers:** the stream plays in the browser on the device you use. The
optional
[Mamma Mi Radio integration](https://github.com/florianhorner/mammamiradio/blob/main/docs/integrations/ha-integration.md#install-the-hacs-integration-for-ha-native-playback)
from HACS adds it as a media source for Home Assistant speakers.

Configuration, the guided first run, speaker setup and repair are in the
Documentation tab of the **Mamma Mi Radio** app, also online as
[the add-on documentation](https://github.com/florianhorner/mammamiradio/blob/main/ha-addon/mammamiradio/DOCS.md).
