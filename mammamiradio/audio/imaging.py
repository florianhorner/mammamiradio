"""Station imaging selection and synthesis helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.audio.normalizer import (
    _MP3_OUTPUT_ARGS,
    _fmt_num,
    _run_ffmpeg,
    generate_bumper_jingle,
    generate_station_id_bed,
    generate_tone,
    generate_transition_sting,
)
from mammamiradio.audio.synth_cache import duration_bucket_sec, materialize_synth_mp3, next_synth_variant
from mammamiradio.core.models import SegmentType

logger = logging.getLogger(__name__)

_RECIPE_MANIFEST_SCHEMA_VERSION = 2
_MAX_RECIPE_ONESHOTS = 2
_MAX_RECIPE_SIMULTANEOUS_LAYERS = 3
_CACHE_UNSET = object()


@dataclass(frozen=True)
class ResolvedRecipeCue:
    """One dry cue selected from a validated imaging recipe.

    ``anchor`` deliberately stays as the manifest's symbolic string.  The ad
    renderer owns dialogue timing, so the imaging layer must not turn a stable
    editorial anchor (for example ``"after_hook"``) into an invented offset.
    """

    anchor: str
    asset_path: Path
    gain_db: float
    max_duration_sec: float


@dataclass(frozen=True)
class ResolvedAdRecipe:
    """A safe, concrete recipe ready for the ad renderer to time and mix."""

    id: str
    bed_path: Path | None
    bed_gain_db: float
    cues: tuple[ResolvedRecipeCue, ...]

    @property
    def oneshots(self) -> tuple[ResolvedRecipeCue, ...]:
        """Compatibility name for callers that describe cues as one-shots."""
        return self.cues


@dataclass(frozen=True)
class _RecipeAsset:
    """Validated private representation of one manifest asset."""

    id: str
    path: Path
    kind: str
    tags: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RecipeCueDefinition:
    anchor: str
    asset_candidates: tuple[str, ...]
    gain_db: float
    max_duration_sec: float


@dataclass(frozen=True)
class _RecipeDefinition:
    id: str
    bed_candidates: tuple[str, ...]
    bed_gain_db: float
    cues: tuple[_RecipeCueDefinition, ...]


@dataclass(frozen=True)
class _RecipeManifest:
    assets: Mapping[str, _RecipeAsset]
    recipes: Mapping[str, _RecipeDefinition]


class ImagingLibrary:
    """Resolve branded stingers and talk beds, falling back to synthetic audio."""

    def __init__(
        self,
        motif_notes: list[int],
        tmp_dir: Path,
        bed_volume_db: float = -18.0,
        assets_dir: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.motif_notes = motif_notes
        self.tmp_dir = tmp_dir
        self.bed_volume_db = bed_volume_db
        self.assets_dir = assets_dir or Path(__file__).resolve().parent.parent / "assets" / "imaging"
        self.cache_dir = cache_dir
        self._recipe_manifest_signature: object = _CACHE_UNSET
        self._recipe_manifest: _RecipeManifest | None = None
        self._resolved_ad_recipe_cache: dict[tuple[str, str], ResolvedAdRecipe | None] = {}

    def resolve_ad_recipe(self, recipe_id: str, variant_key: str = "") -> ResolvedAdRecipe | None:
        """Resolve one schema-v2 ad recipe without ever trusting pack input.

        The parsed manifest and successful selections are cached by manifest
        metadata plus ``variant_key``.  A missing/corrupt manifest, an unknown
        recipe, or any missing selected asset returns ``None`` so the caller can
        retain the proven legacy ad path instead of risking an audio failure.
        """
        if not isinstance(recipe_id, str) or not recipe_id:
            return None
        safe_variant_key = variant_key if isinstance(variant_key, str) else ""
        manifest = self._load_recipe_manifest()
        if manifest is None:
            return None

        cache_key = (recipe_id, safe_variant_key)
        if cache_key in self._resolved_ad_recipe_cache:
            cached = self._resolved_ad_recipe_cache[cache_key]
            if cached is None:
                return None
            if self._resolved_recipe_assets_exist(cached):
                return cached

        definition = manifest.recipes.get(recipe_id)
        if definition is None:
            self._resolved_ad_recipe_cache[cache_key] = None
            return None

        bed = self._select_recipe_asset(
            manifest.assets,
            definition.bed_candidates,
            recipe_id=definition.id,
            slot="bed",
            variant_key=safe_variant_key,
        )
        if definition.bed_candidates and bed is None:
            self._resolved_ad_recipe_cache[cache_key] = None
            return None

        cues: list[ResolvedRecipeCue] = []
        for cue_index, cue in enumerate(definition.cues):
            asset = self._select_recipe_asset(
                manifest.assets,
                cue.asset_candidates,
                recipe_id=definition.id,
                slot=f"cue:{cue_index}",
                variant_key=safe_variant_key,
            )
            if asset is None:
                self._resolved_ad_recipe_cache[cache_key] = None
                return None
            cues.append(
                ResolvedRecipeCue(
                    anchor=cue.anchor,
                    asset_path=asset.path,
                    gain_db=cue.gain_db,
                    max_duration_sec=cue.max_duration_sec,
                )
            )

        resolved = ResolvedAdRecipe(
            id=definition.id,
            bed_path=bed.path if bed is not None else None,
            bed_gain_db=definition.bed_gain_db,
            cues=tuple(cues),
        )
        self._resolved_ad_recipe_cache[cache_key] = resolved
        return resolved

    def _load_recipe_manifest(self) -> _RecipeManifest | None:
        """Load and cache only a fully validated schema-v2 recipe manifest."""
        manifest_path = self.assets_dir / "manifest.json"
        try:
            stat = manifest_path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None

        if signature == self._recipe_manifest_signature:
            return self._recipe_manifest

        self._recipe_manifest_signature = signature
        self._recipe_manifest = None
        self._resolved_ad_recipe_cache.clear()
        if signature is None or not manifest_path.is_file():
            return None

        try:
            raw_manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable imaging recipe manifest %s: %s", manifest_path, exc)
            return None

        parsed = self._parse_recipe_manifest(raw_manifest)
        if parsed is None:
            logger.warning("Ignoring malformed imaging recipe manifest: %s", manifest_path)
            return None
        self._recipe_manifest = parsed
        return parsed

    def _parse_recipe_manifest(self, raw_manifest: object) -> _RecipeManifest | None:
        if not isinstance(raw_manifest, Mapping):
            return None
        if raw_manifest.get("schema_version") != _RECIPE_MANIFEST_SCHEMA_VERSION:
            return None
        raw_assets = raw_manifest.get("assets")
        raw_recipes = raw_manifest.get("recipes")
        if not isinstance(raw_assets, list) or not isinstance(raw_recipes, list):
            return None

        assets: dict[str, _RecipeAsset] = {}
        for raw_asset in raw_assets:
            asset = self._parse_recipe_asset(raw_asset)
            if asset is None or asset.id in assets:
                return None
            assets[asset.id] = asset

        recipes: dict[str, _RecipeDefinition] = {}
        for raw_recipe in raw_recipes:
            recipe = self._parse_recipe_definition(raw_recipe)
            if recipe is None or recipe.id in recipes:
                return None
            recipes[recipe.id] = recipe
        return _RecipeManifest(assets=assets, recipes=recipes)

    def _parse_recipe_asset(self, raw_asset: object) -> _RecipeAsset | None:
        if not isinstance(raw_asset, Mapping):
            return None
        asset_id = self._nonempty_string(raw_asset.get("id"))
        relative_path = self._nonempty_string(raw_asset.get("path"))
        kind = self._nonempty_string(raw_asset.get("kind"))
        tags = self._string_list(raw_asset.get("tags"))
        if asset_id is None or relative_path is None or kind is None or tags is None:
            return None
        source_ids = self._string_list(raw_asset.get("source_ids", []))
        if source_ids is None:
            return None
        asset_path = self._safe_pack_asset_path(relative_path)
        if asset_path is None:
            return None
        return _RecipeAsset(
            id=asset_id,
            path=asset_path,
            kind=kind,
            tags=tags,
            source_ids=source_ids,
        )

    def _parse_recipe_definition(self, raw_recipe: object) -> _RecipeDefinition | None:
        if not isinstance(raw_recipe, Mapping):
            return None
        recipe_id = self._nonempty_string(raw_recipe.get("id"))
        if recipe_id is None:
            return None

        bed_candidates: tuple[str, ...] = ()
        bed_gain_db = 0.0
        if "bed" in raw_recipe:
            raw_bed = raw_recipe.get("bed")
            if raw_bed is not None:
                if not isinstance(raw_bed, Mapping):
                    return None
                parsed_bed_candidates = self._asset_candidates(raw_bed)
                bed_gain = self._finite_number(raw_bed.get("gain_db"))
                if parsed_bed_candidates is None or bed_gain is None:
                    return None
                bed_candidates = parsed_bed_candidates
                bed_gain_db = bed_gain
        elif "bed_candidates" in raw_recipe:
            parsed_bed_candidates = self._string_list(raw_recipe.get("bed_candidates"))
            if not parsed_bed_candidates:
                return None
            bed_candidates = parsed_bed_candidates

        if "cues" in raw_recipe and "oneshots" in raw_recipe:
            return None
        raw_cues = raw_recipe.get("cues", raw_recipe.get("oneshots"))
        if raw_cues is None and bed_candidates:
            raw_cues = []
        if not isinstance(raw_cues, list) or len(raw_cues) > _MAX_RECIPE_ONESHOTS:
            return None

        cues: list[_RecipeCueDefinition] = []
        for raw_cue in raw_cues:
            cue = self._parse_recipe_cue(raw_cue)
            if cue is None:
                return None
            cues.append(cue)
        if 1 + len(cues) > _MAX_RECIPE_SIMULTANEOUS_LAYERS:
            return None
        return _RecipeDefinition(
            id=recipe_id,
            bed_candidates=bed_candidates,
            bed_gain_db=bed_gain_db,
            cues=tuple(cues),
        )

    def _parse_recipe_cue(self, raw_cue: object) -> _RecipeCueDefinition | None:
        if not isinstance(raw_cue, Mapping):
            return None
        anchor = self._nonempty_string(raw_cue.get("anchor"))
        candidates = self._asset_candidates(raw_cue)
        gain_db = self._finite_number(raw_cue.get("gain_db"))
        max_duration_sec = self._finite_number(raw_cue.get("max_duration_sec"))
        if anchor is None or candidates is None or gain_db is None or max_duration_sec is None or max_duration_sec <= 0:
            return None
        return _RecipeCueDefinition(
            anchor=anchor,
            asset_candidates=candidates,
            gain_db=gain_db,
            max_duration_sec=max_duration_sec,
        )

    @staticmethod
    def _nonempty_string(value: object) -> str | None:
        return value if isinstance(value, str) and value.strip() else None

    @classmethod
    def _string_list(cls, value: object) -> tuple[str, ...] | None:
        if not isinstance(value, list):
            return None
        values = tuple(cls._nonempty_string(item) for item in value)
        if any(item is None for item in values):
            return None
        return tuple(item for item in values if item is not None)

    @staticmethod
    def _finite_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None

    @classmethod
    def _asset_candidates(cls, raw: Mapping[str, object]) -> tuple[str, ...] | None:
        has_asset_id = "asset_id" in raw
        has_candidates = "asset_candidates" in raw
        if has_asset_id == has_candidates:
            return None
        if has_asset_id:
            asset_id = cls._nonempty_string(raw.get("asset_id"))
            return (asset_id,) if asset_id is not None else None
        candidates = cls._string_list(raw.get("asset_candidates"))
        return candidates if candidates else None

    def _safe_pack_asset_path(self, relative_path: str) -> Path | None:
        """Return a real asset only when it resolves inside the configured pack."""
        candidate = Path(relative_path)
        if candidate.is_absolute():
            return None
        try:
            pack_root = self.assets_dir.resolve(strict=False)
            resolved = (pack_root / candidate).resolve(strict=False)
            resolved.relative_to(pack_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _select_recipe_asset(
        self,
        assets: Mapping[str, _RecipeAsset],
        candidates: tuple[str, ...],
        *,
        recipe_id: str,
        slot: str,
        variant_key: str,
    ) -> _RecipeAsset | None:
        available = [asset for asset_id in candidates if (asset := assets.get(asset_id)) and asset.path.is_file()]
        if not available:
            return None
        if len(available) == 1:
            return available[0]
        selector = f"{recipe_id}\x00{slot}\x00{variant_key}".encode()
        index = int.from_bytes(hashlib.sha256(selector).digest()[:8], "big") % len(available)
        return available[index]

    @staticmethod
    def _resolved_recipe_assets_exist(recipe: ResolvedAdRecipe) -> bool:
        paths = [recipe.bed_path] if recipe.bed_path is not None else []
        paths.extend(cue.asset_path for cue in recipe.cues)
        return all(path.is_file() for path in paths)

    def _copy_pack_asset(self, output_path: Path, *relative_paths: str) -> bool:
        """Copy the first matching packaged or operator-provided imaging asset.

        The runtime must never need to render an identity sound just to keep the
        station moving.  Every caller in this class therefore treats the pack as
        an optional, immediate override and retains its existing synthetic
        fallback when an operator supplies an incomplete custom asset directory.
        """
        for relative_path in relative_paths:
            asset = self.assets_dir / relative_path
            if asset.is_file():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(asset, output_path)
                logger.info("Using packaged imaging asset: %s", asset.name)
                return True
        return False

    def ad_sfx_dir(self, configured_dir: Path | None = None) -> Path | None:
        """Return a real custom SFX directory, otherwise the bundled identity pack."""
        if configured_dir is not None and configured_dir.is_dir():
            return configured_dir
        bundled = self.assets_dir / "sfx"
        return bundled if bundled.is_dir() else configured_dir

    def ad_beds_dir(self) -> Path | None:
        """Return the bundled/overridden bed directory when it contains MP3 assets."""
        beds_dir = self.assets_dir / "beds"
        if beds_dir.is_dir() and any(path.is_file() for path in beds_dir.glob("*.mp3")):
            return beds_dir
        return None

    def pick_stinger(self, from_seg: SegmentType, to_seg: SegmentType, output_path: Path) -> Path:
        """Pick or synthesize a transition stinger for a segment boundary."""
        specific_asset = f"stingers/{from_seg.value}_{to_seg.value}.mp3"
        generic_asset = (
            "stingers/music_to_speech.mp3"
            if from_seg == SegmentType.MUSIC
            else "stingers/speech_to_music.mp3"
            if to_seg == SegmentType.MUSIC
            else ""
        )
        if self._copy_pack_asset(output_path, specific_asset, generic_asset):
            return output_path

        params = {
            "from": from_seg.value,
            "motif_notes": self.motif_notes,
            "to": to_seg.value,
        }
        variant = next_synth_variant("transition_sting", params)
        return materialize_synth_mp3(
            self.cache_dir,
            "transition_sting",
            output_path,
            params,
            lambda path: generate_transition_sting(
                from_seg.value, to_seg.value, path, self.motif_notes, variant=variant
            ),
            variant=variant,
        )

    def pick_sweeper_sting(self, output_path: Path) -> Path:
        """Generate the motif underlay used below short station sweepers."""
        if self._copy_pack_asset(output_path, "sweeper.mp3"):
            return output_path
        params = {
            "duration_sec": duration_bucket_sec(2.0),
            "motif_notes": self.motif_notes,
        }
        return materialize_synth_mp3(
            self.cache_dir,
            "sweeper_sting",
            output_path,
            params,
            lambda path: generate_station_id_bed(path, 2.0, self.motif_notes),
        )

    def pick_station_id_bed(self, output_path: Path, duration_sec: float = 3.0) -> Path:
        """Pick the canonical station-ID bed, falling back to the configured motif."""
        if self._copy_pack_asset(output_path, "station_id.mp3"):
            return output_path
        return generate_station_id_bed(output_path, duration_sec, self.motif_notes)

    def pick_time_check_sting(self, output_path: Path) -> Path:
        """Pick the short time-check cue without introducing a render dependency."""
        if self._copy_pack_asset(output_path, "time_check.mp3"):
            return output_path
        return generate_tone(output_path, freq_hz=1047, duration_sec=0.3)

    def pick_ad_bumper(self, output_path: Path, duration_sec: float = 1.5) -> Path:
        """Pick the master ad-break bumper, retaining the procedural fallback."""
        if self._copy_pack_asset(output_path, "bumpers/ad_break.mp3"):
            return output_path
        return generate_bumper_jingle(output_path, duration_sec)

    def pick_talk_bed(
        self,
        duration_sec: float,
        output_path: Path,
        source_track: Path | None = None,
    ) -> Path:
        """Pick or synthesize a quiet bed for spoken segments."""
        duration = max(float(duration_sec), 0.5)
        beds_dir = self.assets_dir / "beds"
        if beds_dir.is_dir():
            candidates = sorted(p for p in beds_dir.glob("*.mp3") if p.is_file())
            if candidates:
                return self._loop_bed(random.choice(candidates), duration, output_path)

        if source_track is not None:
            if source_track.exists():
                return self._loop_bed(source_track, duration, output_path)
            logger.warning("pick_talk_bed: source_track %s not found, using synthetic drone", source_track.name)

        if self.cache_dir is None:
            return self._generate_synthetic_drone(duration, output_path)

        bucket = duration_bucket_sec(duration)
        params = {
            "bed_volume_db": self.bed_volume_db,
            "duration_sec": bucket,
        }
        variant = next_synth_variant("talk_bed", params)
        return materialize_synth_mp3(
            self.cache_dir,
            "talk_bed",
            output_path,
            params,
            lambda path: self._generate_synthetic_drone(float(bucket), path, variant=variant),
            variant=variant,
        )

    def _loop_bed(self, input_path: Path, duration_sec: float, output_path: Path) -> Path:
        filter_chain = (
            f"atrim=0:{_fmt_num(duration_sec)},asetpts=N/SR/TB,loudnorm=I={_fmt_num(self.bed_volume_db)}:LRA=11:TP=-1.5"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(input_path),
            "-vn",
            "-af",
            filter_chain,
            *_MP3_OUTPUT_ARGS,
            "-t",
            _fmt_num(duration_sec),
            str(output_path),
        ]
        _run_ffmpeg(cmd, "loop talk bed")
        return output_path

    def _generate_synthetic_drone(self, duration_sec: float, output_path: Path, *, variant: int = 0) -> Path:
        variant = max(0, int(variant)) % 3
        root = 130.0 if variant == 0 else 130.0 * (1.0 + 0.025 * variant)
        harmonic = root * 2
        expr = f"0.18*sin(2*PI*{_fmt_num(root)}*t)+0.07*sin(2*PI*{_fmt_num(harmonic)}*t)"
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={expr}|{expr}:d={_fmt_num(duration_sec)}:s=48000:c=stereo",
            "-af",
            f"loudnorm=I={_fmt_num(self.bed_volume_db)}:LRA=11:TP=-1.5",
            *_MP3_OUTPUT_ARGS,
            "-t",
            _fmt_num(duration_sec),
            str(output_path),
        ]
        _run_ffmpeg(cmd, "synthetic talk bed")
        return output_path
