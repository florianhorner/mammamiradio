"""Guard that admin discard-reason labels stay in lockstep with the Python enum."""

from __future__ import annotations

import re
from pathlib import Path

from mammamiradio.core.models import GenerationWasteReason

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"


def _python_reasons() -> set[str]:
    return {
        value
        for name, value in vars(GenerationWasteReason).items()
        if name.isupper() and isinstance(value, str)
    }


def _js_reason_labels() -> set[str]:
    text = ADMIN_HTML.read_text(encoding="utf-8")
    start = text.find("REASON_LABELS={")
    assert start != -1, "REASON_LABELS={ not found in admin.html"
    end = text.find("};", start)
    assert end != -1, "closing }; of REASON_LABELS not found in admin.html"
    blob = text[start:end]
    keys = set(re.findall(r"^\s*([a-z_]+)\s*:", blob, re.MULTILINE))
    assert keys, (
        "extracted REASON_LABELS set is empty — the object may have been renamed"
    )
    return keys


def test_reason_labels_match_generation_waste_reasons() -> None:
    py_reasons = _python_reasons()
    js_labels = _js_reason_labels()

    missing_labels = py_reasons - js_labels
    dead_labels = js_labels - py_reasons

    assert not missing_labels, (
        "a discard reason would render with no human label: "
        f"{sorted(missing_labels)}"
    )
    assert not dead_labels, (
        f"a label is dead (no matching GenerationWasteReason constant): "
        f"{sorted(dead_labels)}"
    )
