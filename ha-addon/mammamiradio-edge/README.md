# Mamma Mi Radio (Edge)

> **The development channel — not the one to listen to every day.** It is
> pinned to the newest tested `main` image available when the maintainer cuts
> an Edge release, so it may trail `main`. Use it to try changes early and help
> catch problems; install **Mamma Mi Radio** for daily listening. The two
> cannot run at the same time, because they share port 8000.

<!-- shared-listing-body: byte-identical across Stable and Edge, enforced by scripts/validate-addon.sh -->

![The Mamma Mi Radio listener page](https://raw.githubusercontent.com/florianhorner/mammamiradio/main/docs/screenshots/listener.png)

Turn your Home Assistant home into a living radio show. Marco and Giulia
introduce the music, talk over each other, and cut to gloriously fake ads. If
you choose, selected moments from your home join the programme.

Press start and hear the authored opening on a real speaker without a provider
key or Home context. Add generated hosts and a filtered Home preview later.

## What you get

- A continuous music stream with hosts between the songs
- Italian ad breaks for brands that are pure fiction — written fresh each time
  once you add an AI host key, and drawn from stock copy until you do
- A listener page anyone on your network can open, and a control room for you
- Hosts who can mention what your home is actually doing — off until you see a
  preview and choose to allow it

## Playing it on your speakers

Install the [Mamma Mi Radio
integration](https://github.com/florianhorner/mammamiradio/blob/main/docs/integrations/ha-integration.md#install-the-hacs-integration-for-speaker-playback)
through HACS and restart Home Assistant once. The station then shows up as a
media source you can send to any speaker in the house. Without the integration,
the stream still plays directly.

## Everything else

Configuration, the guided first run, speaker setup, and what to do when
something sounds wrong are covered in [the
documentation](https://github.com/florianhorner/mammamiradio/blob/main/ha-addon/mammamiradio/DOCS.md).
