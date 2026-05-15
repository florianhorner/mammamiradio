"""Station imaging selection and synthesis helpers."""

from __future__ import annotations

import logging
import random
import shutil
from pathlib import Path

from mammamiradio.audio.normalizer import (
    _MP3_OUTPUT_ARGS,
    _fmt_num,
    _run_ffmpeg,
    generate_station_id_bed,
    generate_transition_sting,
)
from mammamiradio.core.models import SegmentType

logger = logging.getLogger(__name__)


class ImagingLibrary:
    """Resolve branded stingers and talk beds, falling back to synthetic audio."""

    def __init__(
        self,
        motif_notes: list[int],
        tmp_dir: Path,
        bed_volume_db: float = -18.0,
        assets_dir: Path | None = None,
    ) -> None:
        self.motif_notes = motif_notes
        self.tmp_dir = tmp_dir
        self.bed_volume_db = bed_volume_db
        self.assets_dir = assets_dir or Path(__file__).resolve().parent.parent / "assets" / "imaging"

    def pick_stinger(self, from_seg: SegmentType, to_seg: SegmentType, output_path: Path) -> Path:
        """Pick or synthesize a transition stinger for a segment boundary."""
        asset = self.assets_dir / "stingers" / f"{from_seg.value}_{to_seg.value}.mp3"
        if asset.exists():
            shutil.copy2(asset, output_path)
            return output_path

        return generate_transition_sting(from_seg.value, to_seg.value, output_path, self.motif_notes)

    def pick_sweeper_sting(self, output_path: Path) -> Path:
        """Generate the motif underlay used below short station sweepers."""
        return generate_station_id_bed(output_path, 2.0, self.motif_notes)

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

        return self._generate_synthetic_drone(duration, output_path)

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

    def _generate_synthetic_drone(self, duration_sec: float, output_path: Path) -> Path:
        expr = "0.18*sin(2*PI*130*t)+0.07*sin(2*PI*260*t)"
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
