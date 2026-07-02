"""Shared release-beat manifest schema constants."""

import re

VALID_CHANNELS = frozenset({"edge", "stable"})
VALID_PRIORITIES = frozenset({"low", "normal", "high"})

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{5,120}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

RUNTIME_CONSUMED_KEYS = frozenset(
    {
        "enabled",
        "id",
        "channel",
        "build_sha",
        "semver",
        "title",
        "facts",
        "props",
        "copy_guidance",
        "copy",
        "forbidden_terms",
        "avoid",
        "max_airings",
        "campaign_window_seconds",
        "min_seconds_between_airings",
        "min_segments_between_airings",
    }
)

# Validator-enforced release metadata that the runtime loader intentionally
# ignores. Keeping this explicit makes absence from ReleaseBeatManifest.from_dict
# auditable instead of accidental drift.
VALIDATOR_ONLY_KEYS = frozenset(
    {
        "schema",
        "priority",
        "listener_safe_terms",
    }
)

ALLOWED_KEYS = RUNTIME_CONSUMED_KEYS | VALIDATOR_ONLY_KEYS
