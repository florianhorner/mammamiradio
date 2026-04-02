from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_addon_dockerfile_preserves_package_directory():
    dockerfile = (REPO_ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "COPY pyproject.toml ./\nCOPY mammamiradio/ ./mammamiradio/" in dockerfile
    assert "COPY pyproject.toml mammamiradio/ ./" not in dockerfile, "single COPY would flatten package dir"
