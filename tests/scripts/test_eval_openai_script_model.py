from __future__ import annotations

from types import SimpleNamespace

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


def test_eval_harness_default_models_come_from_registry_api(monkeypatch) -> None:
    expected = ["registry-openai-large", "registry-openai-small"]
    config = SimpleNamespace(models=SimpleNamespace(default_openai_eval_models=lambda: expected))
    monkeypatch.setattr(eval_script, "load_config", lambda path: config)

    assert eval_script.default_models() == expected


def test_eval_harness_cost_uses_registry_price_and_unknown_fallback() -> None:
    seen: list[str] = []

    def price_for_model(model: str) -> tuple[float, float, bool]:
        seen.append(model)
        return (0.000015, 0.000075, True)

    config = SimpleNamespace(models=SimpleNamespace(price_for_model=price_for_model))

    cost, unpriced = eval_script.estimate_cost_usd(
        "experimental-model",
        1_000_000,
        1_000_000,
        config=config,
    )

    assert seen == ["experimental-model"]
    assert cost == 90.0
    assert unpriced is True


def test_eval_harness_uses_registry_defaults_unless_models_are_explicit(monkeypatch, tmp_path) -> None:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text('[{"id":"one","caller":"banter","max_tokens":100,"prompt":"Return JSON: {}"}]')
    seen: list[list[str]] = []

    async def fake_run_all(models, _fixtures):
        seen.append(models)
        return []

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "default_models", lambda: ["registry-default"])
    monkeypatch.setattr(eval_script, "run_all", fake_run_all)

    assert eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(tmp_path / "registry")]) == 0
    assert seen == [["registry-default"]]

    assert (
        eval_script.main(
            [
                "--fixtures",
                str(fixtures),
                "--output-dir",
                str(tmp_path / "explicit"),
                "--models",
                "explicit-candidate",
            ]
        )
        == 0
    )
    assert seen == [["registry-default"], ["explicit-candidate"]]
