"""Constants for the Mamma Mi Radio integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "mammamiradio"

# Config-entry keys.
CONF_HOST = "host"
CONF_PORT = "port"
CONF_ADMIN_TOKEN = "admin_token"

# Default points at the add-on as HA Core resolves it over the Supervisor
# `hassio` bridge: the add-on's bound port is reachable by its slug-hostname
# (underscores -> hyphens) regardless of ingress. Container installs override
# this with the Docker service name (e.g. http://mammamiradio:8000).
DEFAULT_HOST = "local-mammamiradio"
DEFAULT_PORT = 8000

# The add-on polls fast; the read endpoint sets Cache-Control max-age=2.
UPDATE_INTERVAL = timedelta(seconds=5)
HTTP_TIMEOUT = 8.0

# Read contract (unauthenticated) the coordinator polls.
NOW_PLAYING_PATH = "/api/integrations/v1/now-playing"
STREAM_PATH = "/stream"

# Control endpoints (admin-authenticated via the X-Radio-Admin-Token header).
ENDPOINT_PLAY = "/api/resume"
ENDPOINT_STOP = "/api/stop"
ENDPOINT_NEXT = "/api/skip"
ADMIN_TOKEN_HEADER = "X-Radio-Admin-Token"

# Repairs issue identifiers.
ISSUE_ADMIN_TOKEN_REJECTED = "admin_token_rejected"
ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT = "legacy_media_player_push_conflict"
ISSUE_STATION_UNREACHABLE = "station_unreachable"

# Sustained-outage gate for the "station unreachable" repair: only raise after
# this many consecutive failed polls spanning at least this long, so a brief
# blip on a flaky LAN doesn't flap the Repairs dashboard. Startup outages stay
# the job of ConfigEntryNotReady (HA already shows the entry as retrying).
UNREACHABLE_MIN_FAILURES = 3
UNREACHABLE_MIN_SECONDS = 60.0

# Station-logo fallback for entity artwork when a segment has no real cover
# (voice/ad/idle). Matches the legacy ghost push so the card never shows the
# previous track's art during a news flash. Absolute + public so HA can fetch it.
STATION_LOGO_URL = "https://raw.githubusercontent.com/florianhorner/mammamiradio/main/ha-addon/mammamiradio/logo.png"
