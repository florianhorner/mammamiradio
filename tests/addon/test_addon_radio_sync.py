import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CAPELLISSIMO_TAGLINE = "Capellissimo. I capelli che hai sempre sognato. Circa."


def test_addon_radio_toml_matches_root():
    root = (_REPO_ROOT / "radio.toml").read_text(encoding="utf-8")
    addon = (_REPO_ROOT / "ha-addon/mammamiradio/radio.toml").read_text(encoding="utf-8")
    assert addon == root


def test_capellissimo_remains_the_intentional_pharma_hair_gag():
    """The medicine-style disclaimer on a hair brand is deliberate surreal comedy."""
    for path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        config = tomllib.loads((_REPO_ROOT / path).read_text(encoding="utf-8"))
        capellissimo = next(brand for brand in config["ads"]["brands"] if brand["name"] == "Capellissimo")

        assert capellissimo["category"] == "pharma"
        assert capellissimo["tagline"] == _CAPELLISSIMO_TAGLINE
