#!/usr/bin/env python3
"""Tiny mock Home Assistant REST API for staging showreel captures.

The station's REAL home-awareness code (mammamiradio/home/ha_context.py) polls a
Home Assistant instance over its REST API. This serves a *staged* home so the
producer genuinely derives a home mood + summary and the hosts weave it into
banter — without a real HA, and without leaking any real-home telemetry.

It implements exactly the two calls fetch_home_context makes:
  GET  /api/states                          -> list of entity state objects
  POST /api/services/weather/get_forecasts  -> hourly forecast (return_response)
plus GET /api/ as a liveness probe.

Scenarios are plain dicts (entity_id -> {state, attributes}) chosen so the real
classify_home_mood() returns the intended mood. The default scenario "coffee"
yields "Caffè in preparazione" (sensor.kuche_kaffeemaschine_steckdose_power > 50).

States are mutable at runtime via a local control endpoint, so a capture can
stage a *transition* (the station derives events by diffing consecutive polls —
a reactive trigger like the door unlock only fires on a state change):

    POST /__set  {"entity_id": "lock.lock_ultra_8d3c", "state": "unlocked"}

Usage:
    python scripts/showreel/mock_ha.py --port 8123 --scenario coffee
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Entity ids below are the ones classify_home_mood() / the summary builder key on
# (see mammamiradio/home/ha_context.py). The friendly names are what surface in
# the prompt's home_state_data block — keep them fictional/illustrative.
SCENARIOS: dict[str, dict[str, dict]] = {
    # Coffee brewing — the "the house knew before we did" impossible moment.
    # sensor.kuche_kaffeemaschine_steckdose_power > 50 -> "Caffè in preparazione".
    # The switch starts OFF so a capture can stage the reactive "coffee just
    # switched on" event via POST /__set (same flow as the homecoming door).
    "coffee": {
        "switch.bar_kaffeemaschine_steckdose": {
            "state": "off",
            "attributes": {"friendly_name": "Presa macchina del caffè"},
        },
        "sensor.kuche_kaffeemaschine_steckdose_power": {
            "state": "118",
            "attributes": {"friendly_name": "Macchina del caffè", "unit_of_measurement": "W"},
        },
        "light.magic_areas_light_groups_kuche_all_lights": {
            "state": "on",
            "attributes": {"friendly_name": "Luci della cucina", "brightness": 170},
        },
        "light.magic_areas_light_groups_wohnzimmer_all_lights": {
            "state": "on",
            "attributes": {"friendly_name": "Luci del soggiorno", "brightness": 90},
        },
        "media_player.wohnzimmer_sonos_arc_lautsprecher": {
            "state": "playing",
            "attributes": {"friendly_name": "Sonos del soggiorno", "media_title": "Mamma Mi Radio"},
        },
        "sensor.soggiorno_temperatura": {
            "state": "21.5",
            "attributes": {"friendly_name": "Temperatura soggiorno", "unit_of_measurement": "°C"},
        },
        "cover.cucina_finestra": {
            "state": "open",
            "attributes": {"friendly_name": "Finestra della cucina"},
        },
        "weather.forecast_home": {
            "state": "partlycloudy",
            "attributes": {
                "friendly_name": "Meteo",
                "temperature": 24,
                "temperature_unit": "°C",
                "humidity": 58,
            },
        },
    },
    # Homecoming — the "the radio heard the front door" impossible moment.
    # The lock starts LOCKED; flip it to "unlocked" mid-capture via POST /__set
    # so diff_states produces the unlock event and the REACTIVE_TRIGGERS
    # directive ("bentornato") rides into the next banter. person.florian_horner
    # is staged not_home for an optional follow-up flip (the named-welcome take).
    "homecoming": {
        "lock.lock_ultra_8d3c": {
            "state": "locked",
            "attributes": {"friendly_name": "Porta d'ingresso"},
        },
        "person.florian_horner": {
            "state": "not_home",
            "attributes": {"friendly_name": "Florian"},
        },
        "light.magic_areas_light_groups_wohnzimmer_all_lights": {
            "state": "on",
            "attributes": {"friendly_name": "Luci del soggiorno", "brightness": 90},
        },
        "light.magic_areas_light_groups_kuche_all_lights": {
            "state": "off",
            "attributes": {"friendly_name": "Luci della cucina"},
        },
        "media_player.wohnzimmer_sonos_arc_lautsprecher": {
            "state": "playing",
            "attributes": {"friendly_name": "Sonos del soggiorno", "media_title": "Mamma Mi Radio"},
        },
        "sensor.soggiorno_temperatura": {
            "state": "21.8",
            "attributes": {"friendly_name": "Temperatura soggiorno", "unit_of_measurement": "°C"},
        },
        "weather.forecast_home": {
            "state": "clear-night",
            "attributes": {
                "friendly_name": "Meteo",
                "temperature": 16,
                "temperature_unit": "°C",
                "humidity": 62,
            },
        },
    },
}

# Hourly forecast returned by the weather.get_forecasts service call. Read-only
# "real local forecast" the meteo flash riffs on.
FORECASTS: dict[str, list[dict]] = {
    "coffee": [
        {"datetime": "2026-06-23T10:00:00+00:00", "condition": "sunny", "temperature": 24, "precipitation": 0},
        {"datetime": "2026-06-23T13:00:00+00:00", "condition": "partlycloudy", "temperature": 27, "precipitation": 0},
        {"datetime": "2026-06-23T16:00:00+00:00", "condition": "rainy", "temperature": 22, "precipitation": 3.4},
        {"datetime": "2026-06-23T19:00:00+00:00", "condition": "cloudy", "temperature": 20, "precipitation": 0.5},
    ],
    "homecoming": [
        {"datetime": "2026-06-23T19:00:00+00:00", "condition": "clear-night", "temperature": 16, "precipitation": 0},
        {"datetime": "2026-06-23T22:00:00+00:00", "condition": "clear-night", "temperature": 13, "precipitation": 0},
        {"datetime": "2026-06-24T01:00:00+00:00", "condition": "cloudy", "temperature": 11, "precipitation": 0},
        {"datetime": "2026-06-24T07:00:00+00:00", "condition": "sunny", "temperature": 14, "precipitation": 0},
    ],
}


def make_handler(scenario: str):
    # Deep-ish copy so /__set mutations never leak back into SCENARIOS.
    states = {
        eid: {"state": data["state"], "attributes": dict(data["attributes"])}
        for eid, data in SCENARIOS[scenario].items()
    }
    forecast = FORECASTS.get(scenario, [])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, payload, code=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/states":
                # Rebuilt per request: /__set mutations must show up on the next poll.
                self._send([{"entity_id": eid, **data} for eid, data in states.items()])
            elif path in ("/api/", "/api"):
                self._send({"message": "API running."})
            else:
                self._send({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length", 0))
            body_raw = self.rfile.read(length) if length else b""
            if path == "/api/services/weather/get_forecasts":
                self._send({"response": {"weather.forecast_home": {"forecast": forecast}}})
            elif path == "/__set":
                # Capture-harness control: stage a state transition mid-run.
                try:
                    body = json.loads(body_raw)
                    entity_id, new_state = body["entity_id"], str(body["state"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    self._send({"error": "expected {entity_id, state}"}, 400)
                    return
                if entity_id not in states:
                    self._send({"error": f"unknown entity {entity_id!r} in scenario"}, 404)
                    return
                old = states[entity_id]["state"]
                states[entity_id]["state"] = new_state
                print(f"mock-ha: __set {entity_id}: {old!r} -> {new_state!r}")
                self._send({"ok": True, "entity_id": entity_id, "old": old, "new": new_state})
            else:
                self._send({"result": "ok"})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="Mock Home Assistant REST API for showreel staging.")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--scenario", default="coffee", choices=sorted(SCENARIOS))
    args = ap.parse_args()
    handler = make_handler(args.scenario)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"mock-ha: scenario={args.scenario!r} on http://127.0.0.1:{args.port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
