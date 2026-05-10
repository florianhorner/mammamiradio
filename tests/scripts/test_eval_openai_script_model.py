from __future__ import annotations

from scripts import eval_openai_script_model as eval_script


def test_eval_harness_default_output_dir_avoids_context_runtime_state() -> None:
    assert eval_script.DEFAULT_OUTPUT_DIR == eval_script.REPO_ROOT / "tmp" / "evals"
    assert ".context" not in eval_script.DEFAULT_OUTPUT_DIR.parts


def test_eval_harness_does_not_create_output_dir_before_missing_key_error(monkeypatch, tmp_path) -> None:
    output_dir = tmp_path / "evals"
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text('[{"id":"one","caller":"banter","max_tokens":100,"prompt":"Return JSON: {}"}]')
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rc = eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(output_dir)])

    assert rc == 2
    assert not output_dir.exists()
