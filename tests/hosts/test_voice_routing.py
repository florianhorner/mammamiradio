"""Guards the shipped radio.toml voice routing stays coherent.

These are credential-independent structural invariants (they hold with or
without TTS API keys in the environment, i.e. in CI):

- Every ad voice carries a *canonical* speaker role, so it is actually
  castable. A voice with an unknown role lands in the role index but is never
  requested by the casting engine — dead config.
- Every canonical role is filled exactly once, so role -> voice resolution is
  unambiguous (the role index is last-wins; duplicates silently shadow).
- Every brand campaign ``spokesperson`` resolves: it names a canonical role,
  a configured voice carries that role, and the role appears in at least one
  of the campaign's pooled formats (otherwise the spokesperson is overridden
  to the format default and never sticks).
- No brand carries a ``spokesperson`` key at the top level. The loader only
  reads it from inside ``[ads.brands.campaign]``; a top-level key is silently
  dropped, so the pin never takes effect. This guard reads the raw TOML
  because the dropped key is already gone from the parsed config.

Regression: an earlier edit introduced invented role names (``patriarch``,
``ghost``, ...) and bare brand-level ``spokesperson`` keys. The invented roles
fail ``test_every_ad_voice_has_a_canonical_role``; the misplaced keys fail
``test_no_brand_has_top_level_spokesperson``. Both were silently dropped at
load time, so the new voices never aired.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from mammamiradio.core.config import load_config
from mammamiradio.hosts.ad_creative import (
    _FORMAT_ROLES,
    SPEAKER_ROLES,
    AdFormat,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RADIO_TOML = _REPO_ROOT / "radio.toml"


def _config():
    return load_config(str(_RADIO_TOML))


def test_every_ad_voice_has_a_canonical_role():
    cfg = _config()
    for v in cfg.ads.voices:
        assert v.role in SPEAKER_ROLES, (
            f"ad voice {v.name!r} has role {v.role!r} which is not a canonical "
            f"speaker role {sorted(SPEAKER_ROLES)} — it would never be cast"
        )


def test_each_canonical_role_filled_exactly_once():
    cfg = _config()
    by_role: dict[str, list[str]] = {}
    for v in cfg.ads.voices:
        by_role.setdefault(v.role, []).append(v.name)
    for role in SPEAKER_ROLES:
        names = by_role.get(role, [])
        assert len(names) == 1, (
            f"canonical role {role!r} must map to exactly one voice, got {names} "
            f"(zero = uncastable role; duplicates = silent last-wins shadowing)"
        )


def test_brand_spokesperson_pins_resolve_to_a_voice():
    cfg = _config()
    roles_with_voice = {v.role for v in cfg.ads.voices}
    known_formats = {f.value for f in AdFormat}
    for brand in cfg.ads.brands:
        if not (brand.campaign and brand.campaign.spokesperson):
            continue
        sp = brand.campaign.spokesperson
        assert sp in SPEAKER_ROLES, f"brand {brand.name!r} spokesperson {sp!r} is not a canonical role"
        assert sp in roles_with_voice, f"brand {brand.name!r} spokesperson {sp!r} has no configured ad voice"
        pool = [f for f in (brand.campaign.format_pool or []) if f in known_formats]
        # If a format pool is declared, the pinned role must appear in at least
        # one pooled format, or the casting engine overrides it to the default.
        if pool:
            assert any(sp in _FORMAT_ROLES.get(AdFormat(f), []) for f in pool), (
                f"brand {brand.name!r} spokesperson {sp!r} is absent from every "
                f"pooled format {pool}; it would be overridden and never used"
            )


def test_both_hosts_route_to_elevenlabs():
    """Core deliverable: Marco and Giulia are on their ElevenLabs voices."""
    cfg = _config()
    by_name = {h.name: h for h in cfg.hosts}
    for name in ("Marco", "Giulia"):
        assert name in by_name, f"host {name!r} missing from radio.toml"
        assert by_name[name].engine == "elevenlabs", (
            f"host {name!r} expected engine 'elevenlabs', got {by_name[name].engine!r}"
        )
        assert by_name[name].edge_fallback_voice, f"host {name!r} on a cloud engine must declare an edge_fallback_voice"


def test_no_brand_has_top_level_spokesperson():
    """``spokesperson`` only takes effect inside ``[ads.brands.campaign]``.

    A top-level key is silently dropped by the loader, so the pin never airs.
    This reads the raw TOML because the dropped key is gone from the parsed
    config — it is the only guard that catches the misplaced-key mistake.
    """
    with open(_RADIO_TOML, "rb") as f:
        raw = tomllib.load(f)
    for brand in raw.get("ads", {}).get("brands", []):
        assert "spokesperson" not in brand, (
            f"brand {brand.get('name')!r} declares a top-level 'spokesperson'; "
            f"it must live under [ads.brands.campaign] or it is silently dropped"
        )
