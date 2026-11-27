"""Canonical, dependency-free schema for recorded ad-imaging recipes.

Both the live resolver and the offline public-pack validator use this module.
Keeping the structural contract here means a pack that clears CI cannot later
be rejected or reinterpreted by the on-air renderer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

RECIPE_MANIFEST_SCHEMA_VERSION = 2
MAX_RECIPE_CUES = 2
RECIPE_CUE_ANCHORS = frozenset({"intro", "after_first_voice", "mid", "outro"})
MIN_RECIPE_GAIN_DB = -60.0
MAX_RECIPE_GAIN_DB = 12.0

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class RecipeCueSpec:
    """One canonical, validated foreground cue declaration."""

    anchor: str
    asset_id: str
    gain_db: float
    max_duration_sec: float


@dataclass(frozen=True)
class RecipeSpec:
    """One canonical, validated recorded-imaging recipe declaration."""

    index: int
    id: str
    bed_asset_id: str | None
    bed_gain_db: float | None
    cues: tuple[RecipeCueSpec, ...]


def parse_recipe_specs(
    raw_recipes: object,
    asset_ids: Collection[str],
) -> tuple[tuple[RecipeSpec, ...], tuple[str, ...]]:
    """Parse canonical schema-v2 recipes without touching the filesystem.

    The returned errors are intentionally user-actionable: the public validator
    reports them as build failures, while the runtime can contain a malformed
    custom recipe to that recipe instead of trusting it on air.
    """
    if not isinstance(raw_recipes, list) or not raw_recipes:
        return (), ("recipes must be a non-empty list",)

    declared_assets = set(asset_ids)
    recipe_ids: set[str] = set()
    parsed: list[RecipeSpec] = []
    errors: list[str] = []

    for index, raw_recipe in enumerate(raw_recipes):
        label = f"recipes[{index}]"
        if not isinstance(raw_recipe, Mapping):
            errors.append(f"{label} must be an object")
            continue

        recipe_errors: list[str] = []
        _reject_unknown_fields(raw_recipe, {"id", "bed", "cues"}, label, recipe_errors)
        recipe_id = _identifier(raw_recipe.get("id"), f"{label}.id", recipe_errors)
        if recipe_id is not None:
            if recipe_id in recipe_ids:
                recipe_errors.append(f"{label}.id duplicates recipe {recipe_id!r}")
            else:
                recipe_ids.add(recipe_id)

        bed_asset_id: str | None = None
        bed_gain_db: float | None = None
        if "bed" in raw_recipe:
            raw_bed = raw_recipe["bed"]
            bed_label = f"{label}.bed"
            if not isinstance(raw_bed, Mapping):
                recipe_errors.append(f"{bed_label} must be an object")
            else:
                _reject_unknown_fields(raw_bed, {"asset_id", "gain_db"}, bed_label, recipe_errors)
                bed_asset_id = _asset_id(
                    raw_bed.get("asset_id"), f"{bed_label}.asset_id", declared_assets, recipe_errors
                )
                bed_gain_db = _gain(raw_bed.get("gain_db"), f"{bed_label}.gain_db", recipe_errors)

        raw_cues = raw_recipe.get("cues", [])
        cues: list[RecipeCueSpec] = []
        if not isinstance(raw_cues, list):
            recipe_errors.append(f"{label}.cues must be a list")
        elif len(raw_cues) > MAX_RECIPE_CUES:
            recipe_errors.append(f"{label}.cues must contain at most {MAX_RECIPE_CUES} entries")
        else:
            for cue_index, raw_cue in enumerate(raw_cues):
                cue_label = f"{label}.cues[{cue_index}]"
                if not isinstance(raw_cue, Mapping):
                    recipe_errors.append(f"{cue_label} must be an object")
                    continue
                _reject_unknown_fields(
                    raw_cue,
                    {"anchor", "asset_id", "gain_db", "max_duration_sec"},
                    cue_label,
                    recipe_errors,
                )
                anchor = _anchor(raw_cue.get("anchor"), f"{cue_label}.anchor", recipe_errors)
                asset_id = _asset_id(raw_cue.get("asset_id"), f"{cue_label}.asset_id", declared_assets, recipe_errors)
                gain_db = _gain(raw_cue.get("gain_db"), f"{cue_label}.gain_db", recipe_errors)
                max_duration_sec = _positive_number(
                    raw_cue.get("max_duration_sec"), f"{cue_label}.max_duration_sec", recipe_errors
                )
                if anchor is not None and asset_id is not None and gain_db is not None and max_duration_sec is not None:
                    cues.append(
                        RecipeCueSpec(
                            anchor=anchor,
                            asset_id=asset_id,
                            gain_db=gain_db,
                            max_duration_sec=max_duration_sec,
                        )
                    )

        if bed_asset_id is None and not cues:
            recipe_errors.append(f"{label} must declare a bed, cues, or both")

        if recipe_errors:
            errors.extend(recipe_errors)
            continue
        assert recipe_id is not None
        parsed.append(
            RecipeSpec(
                index=index,
                id=recipe_id,
                bed_asset_id=bed_asset_id,
                bed_gain_db=bed_gain_db,
                cues=tuple(cues),
            )
        )

    return tuple(parsed), tuple(errors)


def _reject_unknown_fields(
    value: Mapping[object, object],
    allowed: set[str],
    label: str,
    errors: list[str],
) -> None:
    unexpected = sorted(str(key) for key in value if key not in allowed)
    if unexpected:
        errors.append(f"{label} contains unsupported field(s): {', '.join(unexpected)}")


def _identifier(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    identifier = value.strip()
    if not _IDENTIFIER_RE.fullmatch(identifier):
        errors.append(f"{label} must use only letters, numbers, dot, dash, and underscore")
        return None
    return identifier


def _asset_id(value: Any, label: str, asset_ids: set[str], errors: list[str]) -> str | None:
    identifier = _identifier(value, label, errors)
    if identifier is not None and identifier not in asset_ids:
        errors.append(f"{label} references undeclared asset {identifier!r}")
        return None
    return identifier


def _anchor(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    anchor = value.strip()
    if anchor not in RECIPE_CUE_ANCHORS:
        errors.append(f"{label} must be one of: {', '.join(sorted(RECIPE_CUE_ANCHORS))}")
        return None
    return anchor


def _gain(value: Any, label: str, errors: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        errors.append(f"{label} must be a finite number")
        return None
    gain = float(value)
    if not MIN_RECIPE_GAIN_DB <= gain <= MAX_RECIPE_GAIN_DB:
        errors.append(f"{label} must be between {MIN_RECIPE_GAIN_DB:g} and {MAX_RECIPE_GAIN_DB:g} dB")
        return None
    return gain


def _positive_number(value: Any, label: str, errors: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        errors.append(f"{label} must be a finite number")
        return None
    number = float(value)
    if number <= 0:
        errors.append(f"{label} must be > 0")
        return None
    return number
