from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_test_addon_local_script_stages_ci_context(tmp_path):
    context_dir = tmp_path / "addon-context"

    result = subprocess.run(
        ["bash", "scripts/test-addon-local.sh", "--stage-only", "--context-dir", str(context_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert (context_dir / "Dockerfile").exists()
    assert (context_dir / "mammamiradio").is_dir()
    assert (context_dir / "mammamiradio" / "__init__.py").exists()
    assert (context_dir / "pyproject.toml").exists()
    assert (context_dir / "radio.toml").exists()


def test_addon_dockerfile_preserves_package_directory():
    dockerfile = (REPO_ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "COPY pyproject.toml ./\nCOPY mammamiradio/ ./mammamiradio/" in dockerfile
    assert "COPY pyproject.toml mammamiradio/ ./" not in dockerfile
