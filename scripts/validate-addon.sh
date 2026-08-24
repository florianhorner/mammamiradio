#!/usr/bin/env bash
# Local smoke test for HA addon — catches 80% of CI failures before push.
# Mirrors the validation steps in .github/workflows/addon-build.yml plus
# additional checks derived from real production failures.
#
# Usage: ./scripts/validate-addon.sh [--build]
#   --build    Also build the Docker image locally (slow, requires Docker)
set -euo pipefail

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/validate-addon.sh [--build]

Local pre-flight for the HA add-on. Mirrors the validation steps in
.github/workflows/addon-build.yml plus checks derived from real production
failures (version sync, image path, options contract, critical files).

Options:
  -h, --help   Show this help and exit
  --build      Also build the Docker image locally (slow, requires Docker)

Runs in pre-commit and pre-push hooks for files that can break the add-on.
EOF
    exit 0
    ;;
esac

RED='\033[0;31m'
BLUE='\033[0;34m'
AMBER='\033[0;33m'
NC='\033[0m'
PASS="${BLUE}PASS${NC}"
FAIL="${RED}FAIL${NC}"
WARN="${AMBER}WARN${NC}"

errors=0
warnings=0

pass() { echo -e "  ${PASS}  $1"; }
fail() { echo -e "  ${FAIL}  $1"; errors=$((errors + 1)); }
warn() { echo -e "  ${WARN}  $1"; warnings=$((warnings + 1)); }
extract_yaml_block() {
    local section="$1"
    local file="$2"
    awk -v section="$section" '
        $0 == section ":" { in_section = 1; next }
        in_section && /^[^[:space:]]/ { exit }
        in_section { print }
    ' "$file"
}
assert_image_recovery_assets() {
    local image="$1"
    if docker run --rm -i "$image" python3 - <<'PY'
from importlib import resources
from pathlib import Path
import subprocess

from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR
from mammamiradio.core.spoken_assets import is_approved_packaged_audio_asset

REQUIRED_RECOVERY_ASSETS = ("continuity_1.mp3", "emergency_tone.mp3")

try:
    recovery_dir = resources.files("mammamiradio").joinpath("assets", "demo", "recovery")
except ModuleNotFoundError as exc:
    raise SystemExit(f"cannot import mammamiradio package: {exc}") from exc

try:
    clips = {
        clip.name: clip
        for clip in recovery_dir.iterdir()
        if clip.is_file() and clip.name.endswith(".mp3")
    }
except (FileNotFoundError, NotADirectoryError, OSError) as exc:
    raise SystemExit(f"missing recovery asset directory in installed image: {exc}") from exc

failures = []
for asset_name in REQUIRED_RECOVERY_ASSETS:
    clip = clips.get(asset_name)
    if clip is None:
        failures.append(f"{asset_name}: missing")
        continue
    try:
        size = len(clip.read_bytes())
    except OSError as exc:
        failures.append(f"{asset_name}: unreadable ({exc})")
        continue
    if size <= 1024:
        failures.append(f"{asset_name}: only {size} bytes")
        continue
    asset_path = Path(DEMO_ASSETS_DIR) / "recovery" / asset_name
    if not is_approved_packaged_audio_asset(asset_path, assets_root=Path(DEMO_ASSETS_DIR)):
        failures.append(f"{asset_name}: manifest/hash validation failed")
        continue

    try:
        with resources.as_file(clip) as path:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except FileNotFoundError as exc:
        raise SystemExit("ffprobe is not installed in the image") from exc
    except subprocess.TimeoutExpired:
        failures.append(f"{asset_name}: ffprobe timed out")
        continue

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode == 0 and "audio" in stdout.lower():
        print(f"recovery asset OK: {asset_name} ({size} bytes)")
        continue
    failures.append(f"{asset_name}: {stderr or stdout or f'ffprobe exit {result.returncode}'}")

if failures:
    raise SystemExit("required recovery asset validation failed: " + "; ".join(failures))
PY
    then
        pass "Every required recovery MP3 is installed, manifest-bound, and ffprobe-playable"
        return 0
    else
        fail "A required recovery MP3 is missing, too small, unapproved, or unplayable"
        return 1
    fi
}

assert_image_imaging_assets() {
    local image="$1"
    if docker run --rm -i "$image" python3 - <<'PY'
from hashlib import sha256
from importlib import resources
import json
from pathlib import PurePosixPath
import re
import subprocess

EXPECTED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "pack",
    "provenance",
    "generated_at",
    "design_direction",
    "production_contract",
    "sources",
    "assets",
    "recipes",
    "quality_guard_allowlist",
    "pack_digest",
    "inventory",
}
EXPECTED_PROVENANCE = (
    "Project-authored deterministic masters with exact layer provenance, retained generator inputs, "
    "and a digest-bound complete-pack listening gate."
)
EXPECTED_DESIGN_DIRECTION = {
    "signature": "neon_relay",
    "atmospheric_character": "velvet_horizon",
    "signature_roles": ["identity.station-id", "identity.sweeper"],
    "foreground_policy": "one unique project-authored source per advertising cue",
    "small_speaker_policy": "recognition remains above 170 Hz",
}
EXPECTED_CORE_ASSETS = {
    "identity.station-id": "station_id.mp3",
    "identity.sweeper": "sweeper.mp3",
    "identity.time-check": "time_check.mp3",
    "transition.music-to-speech": "stingers/music_to_speech.mp3",
    "transition.speech-to-music": "stingers/speech_to_music.mp3",
    "bumper.ad-in": "bumpers/ad_in.mp3",
    "bumper.ad-mid": "bumpers/ad_mid.mp3",
    "bumper.ad-out": "bumpers/ad_out.mp3",
    "bed.casa-notte": "beds/casa_notte.mp3",
}
EXPECTED_COMPATIBILITY_IDS = {
    "compat.chime",
    "compat.ding",
    "compat.cash-register",
    "compat.register-hit",
    "compat.sweep",
    "compat.whoosh",
    "compat.tape-stop",
    "compat.hotline-beep",
    "compat.mandolin-sting",
    "compat.ice-clink",
    "compat.startup-synth",
}
EXPECTED_RECIPE_IDS = {
    "cafe_testimonial",
    "stadium_win",
    "showroom_reveal",
    "bureaucracy_stamp",
    "motorway_pass",
    "late_night_hotline",
    "supermarket_dash",
    "pharmacy_whisper",
    "home_reveal",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def require_path(value, *, label):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SystemExit(f"{label} must be a canonical relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
    ):
        raise SystemExit(f"{label} must be a canonical relative POSIX path")
    return value


def require_sha256(value, *, label):
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise SystemExit(f"{label} must be a lowercase SHA-256 digest")
    return value


def inventory_digest(records):
    digest = sha256()
    for path, checksum in records:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(checksum.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def installed_tree(directory, prefix=""):
    files = {}
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise SystemExit(f"cannot enumerate installed Modern Night Drive pack: {exc}") from exc
    for child in children:
        relative = f"{prefix}/{child.name}" if prefix else child.name
        require_path(relative, label="installed pack path")
        if child.is_dir():
            files.update(installed_tree(child, relative))
        elif child.is_file():
            try:
                files[relative] = sha256(child.read_bytes()).hexdigest()
            except OSError as exc:
                raise SystemExit(f"cannot read installed imaging file {relative}: {exc}") from exc
        else:
            raise SystemExit(f"installed imaging pack contains a special entry: {relative}")
    return files


try:
    imaging_dir = resources.files("mammamiradio").joinpath("assets", "imaging")
    manifest = json.loads(imaging_dir.joinpath("manifest.json").read_text(encoding="utf-8"))
except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError, OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot read installed Modern Night Drive imaging pack: {exc}") from exc

if not isinstance(manifest, dict) or set(manifest) != EXPECTED_TOP_LEVEL_FIELDS:
    raise SystemExit("installed Modern Night Drive manifest has the wrong immutable runtime field set")
if (
    manifest.get("schema_version") != 2
    or manifest.get("pack") != "Mamma Mi Radio — Modern Night Drive"
    or manifest.get("provenance") != EXPECTED_PROVENANCE
):
    raise SystemExit("installed imaging manifest has stale pack identity")
if manifest.get("design_direction") != EXPECTED_DESIGN_DIRECTION:
    raise SystemExit("installed imaging manifest has stale Neon Relay / Velvet Horizon direction")
production_contract = manifest.get("production_contract")
if (
    not isinstance(production_contract, dict)
    or production_contract.get("ad_bumper_roles") != ["in", "mid", "out"]
    or production_contract.get("station_and_sweeper_are_runtime_underlays") is not True
):
    raise SystemExit("installed imaging manifest has the wrong runtime production contract")
require_sha256(manifest.get("pack_digest"), label="pack_digest")

assets = manifest.get("assets")
sources = manifest.get("sources")
recipes = manifest.get("recipes")
if not isinstance(assets, list) or len(assets) != 47:
    raise SystemExit("installed Modern Night Drive manifest must contain exactly 47 assets")
if not isinstance(sources, list) or len(sources) != 47:
    raise SystemExit("installed Modern Night Drive manifest must contain exactly 47 retained sources")
if not isinstance(recipes, list) or len(recipes) != 9:
    raise SystemExit("installed Modern Night Drive manifest must contain exactly nine recipes")

recipe_ids = [entry.get("id") for entry in recipes if isinstance(entry, dict)]
if len(recipe_ids) != len(recipes) or set(recipe_ids) != EXPECTED_RECIPE_IDS or len(set(recipe_ids)) != len(recipe_ids):
    raise SystemExit("installed Modern Night Drive manifest has the wrong recipe inventory")

declared_files = {}
asset_by_id = {}
asset_digest_records = []
asset_output_digests = set()

for index, entry in enumerate(assets):
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        raise SystemExit(f"invalid Modern Night Drive asset entry {index}: {entry!r}")
    asset_id = entry["id"]
    if not asset_id or asset_id in asset_by_id:
        raise SystemExit(f"installed imaging manifest repeats asset id: {asset_id!r}")
    relative = require_path(entry.get("path"), label=f"assets[{index}].path")
    checksum = require_sha256(entry.get("sha256"), label=f"assets[{index}].sha256")
    if relative in declared_files:
        raise SystemExit(f"installed imaging manifest repeats runtime path: {relative}")
    if checksum in asset_output_digests:
        raise SystemExit(f"installed imaging manifest contains exact duplicate outputs: {checksum}")
    asset_output_digests.add(checksum)
    declared_files[relative] = checksum
    asset_by_id[asset_id] = entry
    asset_digest_records.append((relative, checksum, asset_id))

    source_ids = entry.get("source_ids")
    layers = entry.get("layers")
    if (
        not isinstance(source_ids, list)
        or len(source_ids) != 1
        or not isinstance(source_ids[0], str)
        or not isinstance(layers, list)
        or len(layers) != 1
        or not isinstance(layers[0], dict)
        or layers[0].get("source_id") != source_ids[0]
    ):
        raise SystemExit(f"installed imaging asset lacks one exact retained-source layer: {asset_id}")
    declared_format = entry.get("format")
    if not isinstance(declared_format, dict) or any(
        declared_format.get(key) != expected
        for key, expected in {"codec": "mp3", "sample_rate_hz": 48000, "channels": 2}.items()
    ):
        raise SystemExit(f"installed imaging asset has the wrong declared format: {relative}")

    asset = imaging_dir.joinpath(*relative.split("/"))
    try:
        size = len(asset.read_bytes())
    except OSError as exc:
        raise SystemExit(f"cannot read installed imaging asset {relative}: {exc}") from exc
    if size <= 1024:
        raise SystemExit(f"installed imaging asset is missing or too small: {relative}")
    try:
        with resources.as_file(asset) as path:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except FileNotFoundError as exc:
        raise SystemExit("ffprobe is not installed in the image") from exc
    except subprocess.TimeoutExpired:
        raise SystemExit(f"ffprobe timed out for installed imaging asset {relative}")
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed for {relative}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ffprobe returned invalid JSON for {relative}: {exc}") from exc
    streams = payload.get("streams", [])
    stream = streams[0] if streams else {}
    if stream.get("codec_name") != "mp3" or stream.get("sample_rate") != "48000" or stream.get("channels") != 2:
        raise SystemExit(f"installed imaging asset has wrong decoded format {relative}: {stream}")

for asset_id, relative in EXPECTED_CORE_ASSETS.items():
    if asset_id not in asset_by_id or asset_by_id[asset_id].get("path") != relative:
        raise SystemExit(f"installed Modern Night Drive core asset is missing or remapped: {asset_id}")

compatibility_ids = {asset_id for asset_id in asset_by_id if asset_id.startswith("compat.")}
if compatibility_ids != EXPECTED_COMPATIBILITY_IDS:
    raise SystemExit("installed Modern Night Drive compatibility inventory is incomplete or stale")
startup = asset_by_id["compat.startup-synth"]
startup_tags = startup.get("tags")
if not isinstance(startup_tags, list) or not {"synth", "synthesizer", "electronic"}.issubset(set(startup_tags)):
    raise SystemExit("startup_synth is not declared as genuine electronic synthesis")

recipe_asset_ids = set()
recipe_bed_ids = set()
recipe_cue_ids = set()
for recipe in recipes:
    recipe_id = recipe["id"]
    bed = recipe.get("bed")
    cues = recipe.get("cues")
    if not isinstance(bed, dict) or not isinstance(bed.get("asset_id"), str):
        raise SystemExit(f"Modern Night Drive recipe has no declared bed: {recipe_id}")
    bed_id = bed["asset_id"]
    if bed_id in recipe_bed_ids or bed_id not in asset_by_id or asset_by_id[bed_id].get("kind") != "ad_bed":
        raise SystemExit(f"Modern Night Drive recipe has a missing or repeated bed: {recipe_id}")
    if not isinstance(cues, list) or len(cues) != 2:
        raise SystemExit(f"Modern Night Drive recipe must contain exactly two cues: {recipe_id}")
    cue_ids = [cue.get("asset_id") for cue in cues if isinstance(cue, dict)]
    if len(cue_ids) != 2 or len(set(cue_ids)) != 2:
        raise SystemExit(f"Modern Night Drive recipe has an invalid cue pair: {recipe_id}")
    for cue_id in cue_ids:
        if (
            not isinstance(cue_id, str)
            or cue_id in recipe_cue_ids
            or cue_id not in asset_by_id
            or asset_by_id[cue_id].get("kind") != "ad_cue"
        ):
            raise SystemExit(f"Modern Night Drive recipe has a missing or reused cue: {recipe_id}")
        recipe_cue_ids.add(cue_id)
    recipe_bed_ids.add(bed_id)
    recipe_asset_ids.update({bed_id, *cue_ids})
if set(asset_by_id) != {*EXPECTED_CORE_ASSETS, *EXPECTED_COMPATIBILITY_IDS, *recipe_asset_ids}:
    raise SystemExit("installed Modern Night Drive asset roles differ from the approved 47-member contract")

pack_digest = sha256()
for relative, checksum, _asset_id in sorted(asset_digest_records, key=lambda item: (item[0], item[2])):
    pack_digest.update(relative.encode("utf-8"))
    pack_digest.update(b"\0")
    pack_digest.update(checksum.encode("ascii"))
    pack_digest.update(b"\n")
if manifest["pack_digest"] != pack_digest.hexdigest():
    raise SystemExit("installed Modern Night Drive pack digest is stale")

source_by_id = {}
for index, entry in enumerate(sources):
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        raise SystemExit(f"invalid retained source entry {index}: {entry!r}")
    source_id = entry["id"]
    if not source_id or source_id in source_by_id:
        raise SystemExit(f"installed imaging manifest repeats retained source id: {source_id!r}")
    relative = require_path(entry.get("original_path"), label=f"sources[{index}].original_path")
    if not relative.startswith("provenance/source-masters/"):
        raise SystemExit(f"retained source is outside provenance/source-masters: {relative}")
    checksum = require_sha256(entry.get("source_sha256"), label=f"sources[{index}].source_sha256")
    if entry.get("provenance_type") != "project-authored-deterministic-master":
        raise SystemExit(f"retained source has stale provenance: {source_id}")
    if relative in declared_files:
        raise SystemExit(f"installed imaging manifest repeats runtime path: {relative}")
    declared_files[relative] = checksum
    source_by_id[source_id] = entry

referenced_source_ids = set()
for asset_id, entry in asset_by_id.items():
    source_id = entry["source_ids"][0]
    source = source_by_id.get(source_id)
    layer = entry["layers"][0]
    if source is None or layer.get("source_sha256") != source.get("source_sha256"):
        raise SystemExit(f"installed imaging asset has stale retained-source provenance: {asset_id}")
    referenced_source_ids.add(source_id)
if referenced_source_ids != set(source_by_id):
    raise SystemExit("installed imaging source ledger is not one-to-one with the asset inventory")

inventory = manifest.get("inventory")
if not isinstance(inventory, dict) or set(inventory) != {"schema_version", "files", "digest"}:
    raise SystemExit("installed Modern Night Drive manifest has no exact runtime inventory")
if type(inventory.get("schema_version")) is not int or inventory["schema_version"] != 1:
    raise SystemExit("installed Modern Night Drive inventory has the wrong schema")
raw_inventory_files = inventory.get("files")
if not isinstance(raw_inventory_files, list) or not raw_inventory_files:
    raise SystemExit("installed Modern Night Drive runtime inventory is empty")
inventory_records = []
inventory_files = {}
for index, entry in enumerate(raw_inventory_files):
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        raise SystemExit(f"invalid runtime inventory entry {index}: {entry!r}")
    relative = require_path(entry.get("path"), label=f"inventory.files[{index}].path")
    checksum = require_sha256(entry.get("sha256"), label=f"inventory.files[{index}].sha256")
    if relative == "manifest.json" or relative in inventory_files:
        raise SystemExit(f"runtime inventory repeats or self-declares path: {relative}")
    inventory_records.append((relative, checksum))
    inventory_files[relative] = checksum
if [path for path, _checksum in inventory_records] != sorted(inventory_files):
    raise SystemExit("installed Modern Night Drive runtime inventory is not canonically sorted")
if inventory_files.get("README.md") is None or inventory_files.get("ATTRIBUTION.md") is None:
    raise SystemExit("installed Modern Night Drive runtime inventory omits its control files")
if set(inventory_files) != {"README.md", "ATTRIBUTION.md", *declared_files}:
    raise SystemExit("installed runtime inventory differs from controls, outputs, and retained sources")
if any(inventory_files[path] != checksum for path, checksum in declared_files.items()):
    raise SystemExit("installed runtime inventory hashes differ from asset/source metadata")
declared_inventory_digest = require_sha256(inventory.get("digest"), label="inventory.digest")
if declared_inventory_digest != inventory_digest(inventory_records):
    raise SystemExit("installed Modern Night Drive runtime inventory digest is stale")

actual_files = installed_tree(imaging_dir)
actual_files.pop("manifest.json", None)
if actual_files != inventory_files:
    missing = sorted(set(inventory_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(inventory_files))
    changed = sorted(
        path for path in set(actual_files) & set(inventory_files) if actual_files[path] != inventory_files[path]
    )
    raise SystemExit(
        f"installed Modern Night Drive runtime tree differs from its inventory "
        f"(missing={missing}, extra={extra}, changed={changed})"
    )

print(
    "Modern Night Drive imaging pack OK: "
    f"{len(assets)} assets, {len(sources)} retained sources, {len(recipes)} recipes"
)
PY
    then
        pass "Installed Modern Night Drive matches its manifest; FFprobe decodes all 47 assets"
        return 0
    else
        fail "Installed Modern Night Drive validation failed; see the error above"
        return 1
    fi
}

assert_image_model_registry() {
    local image="$1"
    local expected_hash actual_hash

    if ! expected_hash=$($PY -c "from hashlib import sha256; from pathlib import Path; print(sha256(Path('model_registry.toml').read_bytes()).hexdigest())"); then
        fail "Could not hash canonical model_registry.toml"
        return 1
    fi

    if ! actual_hash=$(docker run --rm -i "$image" python3 -c '
from hashlib import sha256
from pathlib import Path

registry = Path("/app/model_registry.toml")
if not registry.is_file():
    raise SystemExit("missing /app/model_registry.toml in installed image")
print(sha256(registry.read_bytes()).hexdigest())
'); then
        fail "Installed model_registry.toml missing or unreadable"
        return 1
    fi

    if [ "$actual_hash" = "$expected_hash" ]; then
        pass "Installed model registry matches canonical root registry"
        return 0
    fi

    fail "Installed model registry drifted from canonical root registry"
    return 1
}

assert_image_external_media_absent() {
    local image="$1"
    if docker run --rm -i "$image" python3 - <<'PY'
import importlib.metadata
import importlib.util
import shutil

failures = []
if importlib.util.find_spec("yt_dlp") is not None:
    failures.append("yt_dlp module is importable")
try:
    importlib.metadata.version("yt-dlp")
except importlib.metadata.PackageNotFoundError:
    pass
else:
    failures.append("yt-dlp distribution is installed")
if shutil.which("yt-dlp") is not None:
    failures.append("yt-dlp executable is on PATH")
if failures:
    raise SystemExit("; ".join(failures))
print("external-media extractor absent")
PY
    then
        pass "Installed add-on image omits yt-dlp distribution, module, and executable"
        return 0
    else
        fail "Installed add-on image unexpectedly contains yt-dlp"
        return 1
    fi
}

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$ROOT"

# Prefer a Python that can actually parse TOML for this repo.
PY=""
for candidate in .venv/bin/python3 python3.11 python3; do
    if "$candidate" -c "import tomllib" >/dev/null 2>&1 || \
       "$candidate" -c "import tomli" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    PY=python3
fi

echo "=== HA Addon Pre-Flight Validation ==="
echo ""

# ---- 1. Version consistency ----
echo "1. Version sync"
ADDON_VER=$(grep '^version:' ha-addon/mammamiradio/config.yaml | awk '{print $2}' | tr -d '"')
PYPROJECT_VER=$(grep '^version' pyproject.toml | head -1 | sed 's/.*= *"//;s/".*//')
if [ "$ADDON_VER" = "$PYPROJECT_VER" ]; then
    pass "config.yaml ($ADDON_VER) == pyproject.toml ($PYPROJECT_VER)"
else
    fail "Version mismatch: config.yaml=$ADDON_VER pyproject.toml=$PYPROJECT_VER"
fi

# ---- 2. Image path format ----
echo "2. Image path"
IMAGE=$(grep '^image:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
# Owner detection (primary: git remote → repository.yaml manifest → gh → unknown).
# Each `$(...)` ends in `|| true` so a failing stage (git with no origin, grep
# with no match) can't trip `set -euo pipefail`: under pipefail the pipeline
# inherits git's non-zero exit, which `set -e` would otherwise treat as fatal.
OWNER=$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|/.*||' || true)
# No remote (fresh/no-remote worktree): the repo manifest is the canonical owner.
# Prefer it over `gh api user`, which returns whoever is logged in — possibly a
# different account than the repo owner, which would make EXPECTED wrong and fail
# a correct config.yaml (the bogus mismatch this fallback exists to prevent).
if [ -z "$OWNER" ]; then
    OWNER=$(grep '^url:' repository.yaml 2>/dev/null | sed 's|.*github.com[:/]||;s|/.*||' || true)
fi
# Last resort if the manifest is missing/malformed: the logged-in gh account.
if [ -z "$OWNER" ]; then
    OWNER=$(gh api user -q .login 2>/dev/null || true)
fi
[ -z "$OWNER" ] && OWNER="unknown"
EXPECTED="ghcr.io/${OWNER}/mammamiradio-addon-{arch}"
if [ "$IMAGE" = "$EXPECTED" ]; then
    pass "Image path: $IMAGE"
else
    fail "Image path mismatch: got '$IMAGE', expected '$EXPECTED'"
fi

# ---- 3. Options contract ----
echo "3. Options contract"
OPTIONS_KEYS=$(sed -n '/^options:/,/^[^ ]/p' ha-addon/mammamiradio/config.yaml | grep -E '^[[:space:]]+[[:alnum:]_]+:' | awk -F: '{print $1}' | tr -d ' ')
SCHEMA_KEYS=$(sed -n '/^schema:/,/^[^ ]/p' ha-addon/mammamiradio/config.yaml | grep -E '^[[:space:]]+[[:alnum:]_]+:' | awk -F: '{print $1}' | tr -d ' ')
FILTERED_SCHEMA_KEYS=$(for key in $SCHEMA_KEYS; do
    if printf '%s\n' "$OPTIONS_KEYS" | grep -qx "$key"; then
        echo "$key"
    fi
done)
OPTIONS_ONLY=""
for key in $OPTIONS_KEYS; do
    if ! printf '%s\n' "$SCHEMA_KEYS" | grep -qx "$key"; then
        OPTIONS_ONLY="$OPTIONS_ONLY $key"
    fi
done
REQUIRED_SCHEMA_ONLY=""
for key in $SCHEMA_KEYS; do
    if ! printf '%s\n' "$OPTIONS_KEYS" | grep -qx "$key"; then
        schema_type=$(sed -n '/^schema:/,/^[^ ]/p' ha-addon/mammamiradio/config.yaml | awk -F: -v key="$key" '$1 ~ "^[[:space:]]+" key "$" {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}')
        case "$schema_type" in
            *\?) ;;
            *) REQUIRED_SCHEMA_ONLY="$REQUIRED_SCHEMA_ONLY $key" ;;
        esac
    fi
done
if [ -n "$OPTIONS_ONLY" ]; then
    fail "Options keys missing from schema:$OPTIONS_ONLY"
elif [ "$FILTERED_SCHEMA_KEYS" != "$OPTIONS_KEYS" ]; then
    fail "options keys must follow schema order"
    echo "    options: $(echo "$OPTIONS_KEYS" | tr '\n' ' ')"
    echo "    schema:  $(echo "$SCHEMA_KEYS" | tr '\n' ' ')"
elif [ -n "$REQUIRED_SCHEMA_ONLY" ]; then
    fail "Schema-only keys must be optional:$REQUIRED_SCHEMA_ONLY"
else
    pass "options keys follow schema order; schema-only keys are optional"
fi

MISSING=""
for key in $SCHEMA_KEYS; do
    if ! grep -q "$key" ha-addon/mammamiradio/rootfs/run.sh; then
        MISSING="$MISSING $key"
    fi
done
if [ -z "$MISSING" ]; then
    pass "All schema keys mapped in run.sh"
else
    fail "Keys missing from run.sh:$MISSING"
fi

# ---- 4. Critical files exist ----
echo "4. Critical files"
for f in mammamiradio/__init__.py radio.toml ha-addon/mammamiradio/Dockerfile \
         model_registry.toml \
         ha-addon/mammamiradio/rootfs/run.sh ha-addon/mammamiradio/config.yaml \
         ha-addon/mammamiradio/build.yaml ha-addon/mammamiradio/apparmor.txt \
         ha-addon/mammamiradio/translations/en.yaml; do
    if [ -f "$f" ]; then
        pass "$f"
    else
        fail "Missing: $f"
    fi
done

# ---- 5. Python import works ----
echo "5. Python import"
if $PY -c "import mammamiradio" 2>/dev/null; then
    pass "import mammamiradio"
else
    warn "import mammamiradio failed (is the venv active?)"
fi

# ---- 6. radio.toml parses ----
echo "6. Config parse"
if $PY -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('radio.toml','rb') as f:
    tomllib.load(f)
" 2>/dev/null; then
    pass "radio.toml is valid TOML"
else
    fail "radio.toml parse error"
fi

echo "6b. radio.toml sync"
if cmp -s radio.toml ha-addon/mammamiradio/radio.toml; then
    pass "ha-addon/mammamiradio/radio.toml matches root radio.toml"
else
    fail "ha-addon/mammamiradio/radio.toml drifted from root radio.toml"
fi

echo "6c. Model registry"
if [ ! -f model_registry.toml ]; then
    fail "Missing canonical model_registry.toml"
elif [ -e ha-addon/mammamiradio/model_registry.toml ]; then
    fail "ha-addon/mammamiradio/model_registry.toml must not be committed; stage the canonical root file at build time"
elif SCHEMA_ERR="$($PY -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('model_registry.toml', 'rb') as f:
    raw = tomllib.load(f)
models = raw.get('models')
if not isinstance(models, dict):
    raise SystemExit('model_registry.toml schema invalid: [models] table is required')
catalog = models.get('catalog')
profiles = models.get('profiles')
routing = models.get('routing') or {}
if not isinstance(catalog, dict) or not catalog:
    raise SystemExit('model_registry.toml schema invalid: [models.catalog] must be non-empty')
if not isinstance(profiles, dict) or not profiles:
    raise SystemExit('model_registry.toml schema invalid: [models.profiles] must be non-empty')
if routing and not isinstance(routing, dict):
    raise SystemExit('model_registry.toml schema invalid: [models.routing] must be a table')
default_routing = {
    'banter': 'creative',
    'news_flash': 'creative',
    'ad': 'creative',
    'transition': 'fast',
    'home_mood': 'fast',
    'memory_extract': 'fast',
    'direction': 'creative',
}
merged_routing = {**default_routing, **{str(task): str(role) for task, role in routing.items()}}
for provider in ('anthropic', 'openai'):
    provider_catalog = catalog.get(provider)
    if not isinstance(provider_catalog, dict) or not provider_catalog:
        raise SystemExit(f'model_registry.toml schema invalid: models.catalog.{provider} must be non-empty')
for profile_name, profile_data in profiles.items():
    if not isinstance(profile_data, dict):
        raise SystemExit(f'model_registry.toml schema invalid: profile {profile_name!r} must be a table')
    for provider in ('anthropic', 'openai'):
        provider_profile = profile_data.get(provider)
        if not isinstance(provider_profile, dict):
            raise SystemExit(
                f'model_registry.toml schema invalid: models.profiles.{profile_name}.{provider} must be a table'
            )
        for task, role in merged_routing.items():
            key = provider_profile.get(role)
            model_id = catalog[provider].get(key) if key else None
            if not isinstance(model_id, str) or not model_id.strip():
                raise SystemExit(
                    f'model_registry.toml schema invalid: {profile_name}/{provider}/{task} role {role!r} '
                    'does not resolve to a model id'
                )
tts = raw.get('tts')
openai_tts = tts.get('openai') if isinstance(tts, dict) else None
if not isinstance(openai_tts, dict) or not str(openai_tts.get('model', '')).strip():
    raise SystemExit('model_registry.toml schema invalid: [tts.openai].model must be non-empty')
pricing = raw.get('pricing')
if not isinstance(pricing, dict):
    raise SystemExit('model_registry.toml schema invalid: [pricing] table is required')
fallback_input = float(pricing.get('fallback_input_per_million', 0.0))
fallback_output = float(pricing.get('fallback_output_per_million', 0.0))
if fallback_input <= 0 or fallback_output <= 0:
    raise SystemExit('model_registry.toml schema invalid: pricing fallback rates must be positive')
" 2>&1)"; then
    pass "model_registry.toml is valid and remains root-canonical"
else
    fail "model_registry.toml parse/schema error: ${SCHEMA_ERR:-unknown}"
fi

# ---- 7. run.sh syntax check ----
echo "7. Shell syntax"
if bash -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
    pass "run.sh bash syntax OK"
else
    fail "run.sh has bash syntax errors"
fi

if command -v ash >/dev/null 2>&1; then
    if ash -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
        pass "run.sh ash syntax OK"
    else
        fail "run.sh has ash syntax errors"
    fi
elif sh -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
    pass "run.sh POSIX shell syntax OK"
else
    fail "run.sh has POSIX shell syntax errors"
fi

# ---- 8. Hardcoded value sync ----
echo "8. Hardcoded value sync"

STABLE_STAGE=$(grep '^stage:' ha-addon/mammamiradio/config.yaml | awk '{print $2}' | tr -d '"')
if [ "$STABLE_STAGE" = "stable" ]; then
    pass "stable add-on stage: stable"
else
    fail "stable add-on stage must be stable, got: ${STABLE_STAGE:-missing}"
fi

# Port 8000 must appear in config.yaml, run.sh
PORT_CONFIG=$(grep 'ingress_port:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
PORT_RUNSH=$(grep 'MAMMAMIRADIO_PORT=' ha-addon/mammamiradio/rootfs/run.sh | head -1 | sed 's/.*="//' | tr -d '"')
if [ "$PORT_CONFIG" = "8000" ] && [ "$PORT_RUNSH" = "8000" ]; then
    pass "Port 8000 consistent (config.yaml, run.sh)"
else
    fail "Port mismatch: config.yaml=$PORT_CONFIG run.sh=$PORT_RUNSH"
fi

# host_network required for stream access
if grep -q 'host_network: true' ha-addon/mammamiradio/config.yaml; then
    pass "host_network: true (required for local network stream access)"
else
    fail "host_network must be true for local network stream access"
fi

# timeout >= 120 (addon needs time to install Python deps)
TIMEOUT=$(grep '^timeout:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
if [ "${TIMEOUT:-0}" -ge 120 ] 2>/dev/null; then
    pass "timeout: $TIMEOUT (>= 120)"
else
    fail "timeout should be >= 120, got: ${TIMEOUT:-missing}"
fi

# ---- 9. Translations cover all options ----
echo "9. Translations"
trans_errors=0
for key in $SCHEMA_KEYS; do
    if ! grep -q "$key" ha-addon/mammamiradio/translations/en.yaml 2>/dev/null; then
        fail "Translation missing for option: $key"
        trans_errors=$((trans_errors + 1))
    fi
done
if [ $trans_errors -eq 0 ]; then
    pass "All options have translations"
fi

# ---- 10. No JS string rewriting in ingress prefix injection ----
# Discover _inject_ingress_prefix anywhere under mammamiradio/web/*.py rather
# than hardcoding a single source file (issue #467 — the helper has moved
# modules twice). The Python scan reports one of three sentinel tokens so the
# three outcomes stay an explicit, testable contract instead of overloaded
# exit codes:
#   found + all safe   -> stdout "safe"          -> pass
#   found + any unsafe -> stdout "unsafe:<path>" -> fail (same message as before)
#   not found anywhere -> stdout "none"          -> warn (ingress may not work)
# All matching definitions are validated (not just the first), so a stale
# unsafe copy left behind by a refactor cannot slip through. Each file is
# parsed in isolation: a syntax error in an unrelated web module is skipped
# (ruff/mypy/pytest surface that loudly elsewhere) instead of masquerading as
# an ingress-safety failure.
echo "10. Ingress safety"
if [ ! -d mammamiradio/web ]; then
    fail "Missing mammamiradio/web (cannot run ingress safety check)"
elif CHECK_OUTPUT=$($PY -c "
import ast
from pathlib import Path

targets = []
for path in sorted(Path('mammamiradio/web').glob('*.py')):
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        continue
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == '_inject_ingress_prefix':
            targets.append(node)

if not targets:
    print('none')
    raise SystemExit(0)

for target in targets:
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != 'replace':
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        pattern = first.value
        if pattern.startswith(\"'/\") and pattern != \"'/sw.js'\":
            print(f'unsafe:{pattern}')
            raise SystemExit(0)

print('safe')
" 2>&1); then
    case "$CHECK_OUTPUT" in
        safe)
            pass "Ingress prefix injection only rewrites safe patterns" ;;
        none)
            warn "No _inject_ingress_prefix found (ingress may not work)" ;;
        unsafe:*)
            fail "Ingress safety check failed: rewrites single-quoted JS path: ${CHECK_OUTPUT#unsafe:}" ;;
        *)
            fail "Ingress safety check failed: $CHECK_OUTPUT" ;;
    esac
else
    fail "Ingress safety check failed: $CHECK_OUTPUT"
fi

# ---- 11. Dockerfile doesn't COPY to /data/ ----
echo "11. Dockerfile safety"
if grep -qE '^COPY.*\s/data/' ha-addon/mammamiradio/Dockerfile; then
    fail "Dockerfile COPYs to /data/ — this overwrites persistent volumes on update"
else
    pass "No COPY to /data/ (persistent volume safe)"
fi

# shellcheck disable=SC2016  # Match literal Dockerfile ARG references.
for label in 'io.hass.version="${BUILD_VERSION}"' 'io.hass.type="app"' 'io.hass.arch="${BUILD_ARCH}"'; do
    if grep -Fq "$label" ha-addon/mammamiradio/Dockerfile; then
        pass "Dockerfile label: $label"
    else
        fail "Dockerfile missing required Home Assistant image label: $label"
    fi
done

# No bare eval 2>&1 in run.sh (subshell captures like SYNC_MSG="$(...2>&1)" are safe)
# Collapse continuation lines, then reject 2>&1 that is NOT inside a $() capture.
# shellcheck disable=SC2016  # The grep patterns intentionally match literal command substitutions.
UNSAFE_2_1=$(awk '/\\$/{buf=buf $0; next} {if(buf){print buf $0; buf=""} else print}' \
    ha-addon/mammamiradio/rootfs/run.sh | grep '2>&1' | grep -v '"\$(' | grep -v "'\$(" || true)
if [ -n "$UNSAFE_2_1" ]; then
    fail "run.sh uses bare 2>&1 outside subshell capture — stderr injection risk"
else
    pass "No unsafe 2>&1 in eval context"
fi

# Both current channels share one image, so neither may gain extractor
# authority through package metadata, a build extra, or an inherited env var.
if $PY - <<'PY'
from pathlib import Path
import re

try:
    import tomllib
except ImportError:
    import tomli as tomllib

raw = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
base = raw.get("project", {}).get("dependencies", [])
extras = raw.get("project", {}).get("optional-dependencies", {})
if any(str(dep).split("[", 1)[0].lower().startswith("yt-dlp") for dep in base):
    raise SystemExit("yt-dlp must not be a base dependency")
external = extras.get("external-media", [])
if not any(str(dep).lower().startswith("yt-dlp") for dep in external):
    raise SystemExit("external-media extra must own yt-dlp")

requirements_path = Path("requirements.txt")
if not requirements_path.is_file():
    raise SystemExit("requirements.txt is missing")
for number, raw_line in enumerate(requirements_path.read_text(encoding="utf-8").splitlines(), start=1):
    line = raw_line.strip()
    if not line or line.startswith(("#", "--hash=", "\\")):
        continue
    if re.search(r"(?i)(?<![A-Za-z0-9])yt[-_.]+dlp(?![A-Za-z0-9])", line):
        raise SystemExit(f"requirements.txt:{number} must not install yt-dlp")
PY
then
    pass "yt-dlp is standalone-only in the external-media extra and absent from requirements.txt"
else
    fail "yt-dlp dependency boundary is invalid"
fi

if grep -Fq 'export MAMMAMIRADIO_ALLOW_YTDLP="false"' ha-addon/mammamiradio/rootfs/run.sh \
   && ! grep -Fq 'export MAMMAMIRADIO_ALLOW_YTDLP="true"' ha-addon/mammamiradio/rootfs/run.sh \
   && ! grep -Fq '.[external-media]' ha-addon/mammamiradio/Dockerfile; then
    pass "Shared add-on entrypoint hard-disables external media"
else
    fail "Shared add-on image must omit and hard-disable external media"
fi

DIRECT_EXTRACTOR_IMPORTS=$(grep -R -nE '(^|[[:space:]])(import yt_dlp|from yt_dlp)' \
    mammamiradio --include='*.py' | grep -v '^mammamiradio/playlist/downloader.py:' || true)
if [ -z "$DIRECT_EXTRACTOR_IMPORTS" ]; then
    pass "Optional extractor imports stay behind the lazy gateway"
else
    fail "Direct yt_dlp import found outside playlist/downloader.py"
    echo "$DIRECT_EXTRACTOR_IMPORTS"
fi

# ---- 12. repository.yaml on main ----
echo "12. Repository discovery"
if [ -f repository.yaml ]; then
    pass "repository.yaml exists"
else
    fail "repository.yaml missing (HA can't discover the addon repo)"
fi

# ---- 13. Edge add-on folder ----
# The edge add-on runs the SAME image as stable, so its config must stay
# schema-locked to stable. Its version is a manual edge release (`make edge-release`).
echo "13. Edge add-on"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"
STABLE_CONFIG="ha-addon/mammamiradio/config.yaml"
STABLE_TRANS="ha-addon/mammamiradio/translations/en.yaml"
EDGE_TRANS="ha-addon/mammamiradio-edge/translations/en.yaml"
EXPECTED_BACKUP_MODE="backup: hot"
EXPECTED_BACKUP_EXCLUDE_BLOCK=$(cat <<'EOF'
  - "tmp"
  - "cache/.ytdlp_tmp"
  - "cache/restart_handoff"
  - "cache/clips"
  - "cache/captures"
  - "cache/*.mp3"
  - "cache/*.mp3.json"
  - "cache/*.m4a"
  - "cache/*.webm"
  - "*.part"
  - "*.ytdl"
  - "*.tmp"
EOF
)

STABLE_BACKUP_MODE=$(grep '^backup:' "$STABLE_CONFIG" || true)
STABLE_BACKUP_EXCLUDE_COUNT=$(grep -c '^backup_exclude:$' "$STABLE_CONFIG" || true)
STABLE_BACKUP_EXCLUDE_BLOCK=$(extract_yaml_block backup_exclude "$STABLE_CONFIG")
if [ "$STABLE_BACKUP_MODE" = "$EXPECTED_BACKUP_MODE" ]; then
    pass "stable backup mode: hot"
else
    fail "stable backup mode must be hot"
fi
if [ "$STABLE_BACKUP_EXCLUDE_COUNT" = "1" ] && \
   [ "$STABLE_BACKUP_EXCLUDE_BLOCK" = "$EXPECTED_BACKUP_EXCLUDE_BLOCK" ]; then
    pass "stable backup exclusion contract matches expected paths"
else
    fail "stable backup exclusion contract drifted"
fi

if [ ! -f "$EDGE_CONFIG" ]; then
    echo "  (no edge add-on — skipping)"
else
    # YAML validity (best-effort — pyyaml may not be installed)
    if $PY -c "import yaml" 2>/dev/null; then
        if $PY -c "import yaml,sys; yaml.safe_load(open('$EDGE_CONFIG'))" 2>/dev/null; then
            pass "edge config.yaml is valid YAML"
        else
            fail "edge config.yaml YAML parse error"
        fi
    else
        warn "pyyaml not available — skipped edge YAML parse check"
    fi

    # slug
    EDGE_SLUG=$(grep '^slug:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')
    if [ "$EDGE_SLUG" = "mammamiradio-edge" ]; then
        pass "edge slug: $EDGE_SLUG"
    else
        fail "edge slug must be 'mammamiradio-edge', got '$EDGE_SLUG'"
    fi

    # image path — must match stable's expected image (shared repo)
    EDGE_IMAGE=$(grep '^image:' "$EDGE_CONFIG" | awk '{print $2}')
    if [ "$EDGE_IMAGE" = "$EXPECTED" ]; then
        pass "edge image: $EDGE_IMAGE"
    else
        fail "edge image mismatch: got '$EDGE_IMAGE', expected '$EXPECTED'"
    fi

    # version — a manual edge release sets this to the main short SHA (7-char hex,
    # see `make edge-release`). The dotted-numeric form is still accepted so the
    # pre-migration calver value validates until the first SHA release is cut.
    EDGE_VER=$(grep '^version:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')
    if echo "$EDGE_VER" | grep -qE '^[0-9a-f]{7}$|^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$'; then
        pass "edge version format: $EDGE_VER"
    else
        fail "edge version must be a 7-char short SHA (make edge-release), got '$EDGE_VER'"
    fi

    EDGE_STAGE=$(grep '^stage:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')
    if [ "$EDGE_STAGE" = "experimental" ]; then
        pass "edge stage: experimental"
    else
        fail "edge stage must stay experimental, got: ${EDGE_STAGE:-missing}"
    fi

    # Hot-backup policy is an exact, ordered contract. Check each manifest
    # independently so identical (common-mode) drift cannot hide behind parity,
    # then check parity so unilateral stable/edge changes are explicit.
    EDGE_BACKUP_MODE=$(grep '^backup:' "$EDGE_CONFIG" || true)
    EDGE_BACKUP_EXCLUDE_COUNT=$(grep -c '^backup_exclude:$' "$EDGE_CONFIG" || true)
    EDGE_BACKUP_EXCLUDE_BLOCK=$(extract_yaml_block backup_exclude "$EDGE_CONFIG")
    if [ "$EDGE_BACKUP_MODE" = "$EXPECTED_BACKUP_MODE" ]; then
        pass "edge backup mode: hot"
    else
        fail "edge backup mode must be hot"
    fi
    if [ "$EDGE_BACKUP_EXCLUDE_COUNT" = "1" ] && \
       [ "$EDGE_BACKUP_EXCLUDE_BLOCK" = "$EXPECTED_BACKUP_EXCLUDE_BLOCK" ]; then
        pass "edge backup exclusion contract matches expected paths"
    else
        fail "edge backup exclusion contract drifted"
    fi
    if [ "$STABLE_BACKUP_MODE" = "$EDGE_BACKUP_MODE" ] && \
       [ "$STABLE_BACKUP_EXCLUDE_COUNT" = "$EDGE_BACKUP_EXCLUDE_COUNT" ] && \
       [ "$STABLE_BACKUP_EXCLUDE_BLOCK" = "$EDGE_BACKUP_EXCLUDE_BLOCK" ]; then
        pass "edge backup contract matches stable"
    else
        fail "edge backup contract drifted from stable"
    fi

    # options + schema parity with stable (edge runs the same image/run.sh).
    # Block equality is byte-exact, so it also guarantees key parity and
    # (via the translations block check below) translation coverage.
    STABLE_OPT_BLOCK=$(extract_yaml_block options "$STABLE_CONFIG")
    EDGE_OPT_BLOCK=$(extract_yaml_block options "$EDGE_CONFIG")
    if [ "$STABLE_OPT_BLOCK" = "$EDGE_OPT_BLOCK" ]; then
        pass "edge options block matches stable"
    else
        fail "edge options block drifted from stable"
    fi

    STABLE_SCHEMA_BLOCK=$(extract_yaml_block schema "$STABLE_CONFIG")
    EDGE_SCHEMA_BLOCK=$(extract_yaml_block schema "$EDGE_CONFIG")
    if [ "$STABLE_SCHEMA_BLOCK" = "$EDGE_SCHEMA_BLOCK" ]; then
        pass "edge schema block matches stable"
    else
        fail "edge schema block drifted from stable"
    fi

    # Translations must be byte-identical to stable. stable's own translation
    # coverage is enforced by check 9, so equality here transitively guarantees
    # the edge translations cover every edge schema key.
    STABLE_TRANSLATIONS=$(extract_yaml_block configuration "$STABLE_TRANS")
    EDGE_TRANSLATIONS=$(extract_yaml_block configuration "$EDGE_TRANS")
    if [ "$STABLE_TRANSLATIONS" = "$EDGE_TRANSLATIONS" ]; then
        pass "edge translations block matches stable"
    else
        fail "edge translations block drifted from stable"
    fi

    # required files
    for f in ha-addon/mammamiradio-edge/icon.png ha-addon/mammamiradio-edge/logo.png \
             ha-addon/mammamiradio-edge/apparmor.txt "$EDGE_TRANS"; do
        if [ -f "$f" ]; then
            pass "$f"
        else
            fail "Missing: $f"
        fi
    done

    if cmp -s ha-addon/mammamiradio/apparmor.txt ha-addon/mammamiradio-edge/apparmor.txt; then
        pass "edge AppArmor profile matches stable"
    else
        fail "edge AppArmor profile drifted from stable"
    fi

    # ingress / network consistency with stable
    if grep -q 'host_network: true' "$EDGE_CONFIG"; then
        pass "edge host_network: true"
    else
        fail "edge host_network must be true"
    fi
    EDGE_PORT=$(grep 'ingress_port:' "$EDGE_CONFIG" | awk '{print $2}')
    if [ "$EDGE_PORT" = "8000" ]; then
        pass "edge ingress_port: 8000"
    else
        fail "edge ingress_port must be 8000, got: ${EDGE_PORT:-missing}"
    fi
    EDGE_TIMEOUT=$(grep '^timeout:' "$EDGE_CONFIG" | awk '{print $2}')
    if [ "${EDGE_TIMEOUT:-0}" -ge 120 ] 2>/dev/null; then
        pass "edge timeout: $EDGE_TIMEOUT (>= 120)"
    else
        fail "edge timeout should be >= 120, got: ${EDGE_TIMEOUT:-missing}"
    fi
fi

# ---- Optional: Docker build ----
if [ "${1:-}" = "--build" ]; then
    echo ""
    echo "=== Docker Build Test ==="

    # Simulate CI: copy source into build context
    TMPCTX=$(mktemp -d)
    trap 'rm -rf "$TMPCTX"' EXIT
    cp -r ha-addon/mammamiradio/* "$TMPCTX/"
    cp -r mammamiradio/ "$TMPCTX/mammamiradio/"
    cp pyproject.toml "$TMPCTX/"
    cp radio.toml "$TMPCTX/"
    cp model_registry.toml "$TMPCTX/"

    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  BUILD_ARCH="amd64"; BASE="ghcr.io/home-assistant/amd64-base:3.20" ;;
        arm64|aarch64) BUILD_ARCH="aarch64"; BASE="ghcr.io/home-assistant/aarch64-base:3.20" ;;
        *) fail "Unknown arch: $ARCH"; exit 1 ;;
    esac

    echo "  Building for $BUILD_ARCH..."
    if docker build "$TMPCTX" \
        --build-arg BUILD_FROM="$BASE" \
        --build-arg BUILD_VERSION="$ADDON_VER" \
        --build-arg BUILD_ARCH="$BUILD_ARCH" \
        -t mammamiradio-addon-test:local 2>&1 | tail -5; then
        pass "Docker build succeeded"

        echo "  Checking installed recovery assets..."
        assert_image_recovery_assets "mammamiradio-addon-test:local" || true

        echo "  Checking installed Modern Night Drive imaging pack..."
        assert_image_imaging_assets "mammamiradio-addon-test:local" || true

        echo "  Checking installed model registry..."
        assert_image_model_registry "mammamiradio-addon-test:local" || true

        echo "  Checking external-media containment..."
        assert_image_external_media_absent "mammamiradio-addon-test:local" || true

        echo "  Testing container startup..."
        # Create minimal options.json
        echo '{"anthropic_api_key":"","openai_api_key":""}' > "$TMPCTX/options.json"

        CID=$(docker run -d --name mmr-test \
            -v "$TMPCTX/options.json:/data/options.json:ro" \
            -e SUPERVISOR_TOKEN=fake \
            mammamiradio-addon-test:local 2>&1)

        sleep 3
        if docker ps --filter "id=$CID" --filter "status=running" -q | grep -q .; then
            pass "Container running after 3s"
            # Check if uvicorn started
            if docker logs "$CID" 2>&1 | grep -q "Starting uvicorn"; then
                pass "Uvicorn started"
            else
                warn "Uvicorn not yet started (may still be initializing)"
            fi
        else
            fail "Container exited"
            docker logs "$CID" 2>&1 | tail -10
        fi
        docker rm -f mmr-test >/dev/null 2>&1 || true
    else
        fail "Docker build failed"
    fi
fi

# ---- Summary ----
echo ""
echo "=== Results ==="
if [ $errors -eq 0 ]; then
    echo -e "${BLUE}All checks passed.${NC} Safe to push."
else
    echo -e "${RED}$errors check(s) failed.${NC} Fix before pushing."
fi
if [ $warnings -gt 0 ]; then
    echo -e "${AMBER}$warnings warning(s).${NC}"
fi
exit $errors
