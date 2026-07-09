"""Station imaging selection and synthesis helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import random
import shutil
from pathlib import Path
from typing import Any, Mapping

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
