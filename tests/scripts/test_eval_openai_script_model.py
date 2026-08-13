from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from scripts import eval_openai_script_model as eval_script


def _config(*, models: list[str] | None = None, unpriced: bool = False):
    def price_for_model(_model: str) -> tuple[float, float, bool]:
        return (0.000005, 0.00003, unpriced)

    return SimpleNamespace(
        models=SimpleNamespace(
            catalog={"openai": {"large": "registry-large", "small": "registry-small"}},
            default_openai_eval_models=lambda: models or ["registry-large", "registry-small"],
            price_for_model=price_for_model,
        ),
        display_station_name="Mamma Mi Radio",
        hosts=[SimpleNamespace(name="Marco"), SimpleNamespace(name="Giulia")],
        anthropic_api_key="",
        openai_api_key="",
    )


def _fixture_file(
    tmp_path, payload: str = '[{"id":"one","caller":"banter","max_tokens":100,"prompt":"Return JSON: {}"}]'
):
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(payload)
    return fixtures


def test_eval_harness_default_output_dir_avoids_context_runtime_state() -> None:
    assert eval_script.DEFAULT_OUTPUT_DIR == eval_script.REPO_ROOT / "tmp" / "evals"
    assert ".context" not in eval_script.DEFAULT_OUTPUT_DIR.parts


def test_eval_harness_does_not_create_output_dir_before_missing_key_error(monkeypatch, tmp_path) -> None:
    output_dir = tmp_path / "evals"
    fixtures = _fixture_file(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rc = eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(output_dir)])

    assert rc == 2
    assert not output_dir.exists()


def test_eval_harness_default_models_come_from_registry_api(monkeypatch) -> None:
    expected = ["registry-openai-large", "registry-openai-small"]
    config = _config(models=expected)
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


def test_invalid_fixture_fails_before_key_or_output_write(monkeypatch, tmp_path, capsys) -> None:
    fixtures = _fixture_file(tmp_path, "{not json")
    output_dir = tmp_path / "evals"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(output_dir)]) == 2

    assert "fixtures are not valid JSON" in capsys.readouterr().err
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '[{"id":"one","caller":"nope","max_tokens":1,"prompt":"x"}]',
        '[{"id":"one","caller":"banter","max_tokens":0,"prompt":"x"}]',
        # bool must not sneak through as int 1 (True == 1 in Python)
        '[{"id":"one","caller":"banter","max_tokens":true,"prompt":"x"}]',
        '[{"id":"one","caller":"banter","max_tokens":1,"prompt":""}]',
        '[{"id":"one","caller":"banter","max_tokens":1,"prompt":"x"},{"id":"one","caller":"banter","max_tokens":1,"prompt":"x"}]',
    ],
)
def test_invalid_fixture_contract_is_actionable_before_network(monkeypatch, tmp_path, payload) -> None:
    fixtures = _fixture_file(tmp_path, payload)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(tmp_path / "evals")]) == 2


def test_unwritable_output_dir_fails_before_the_paid_run(monkeypatch, tmp_path, capsys) -> None:
    # A bad --output-dir must fail BEFORE any billed call, not after — otherwise the
    # operator pays for the run and then loses every receipt to a post-payment error.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    output_dir = blocker / "evals"  # mkdir(parents=True) can't create under a file
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "load_config", lambda path: _config(models=["only"]))

    async def should_not_run(*args, **kwargs):
        raise AssertionError("run_all must not be called when the output dir is unwritable")

    monkeypatch.setattr(eval_script, "run_all", should_not_run)

    assert eval_script.main(["--fixtures", str(_fixture_file(tmp_path)), "--output-dir", str(output_dir)]) == 2
    assert "cannot create output dir" in capsys.readouterr().err


def test_receipt_open_failure_after_mkdir_fails_before_paid_run(monkeypatch, tmp_path, capsys) -> None:
    output_dir = tmp_path / "evals"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "load_config", lambda path: _config(models=["only"]))

    def fail_open(*args, **kwargs):
        raise PermissionError("receipt blocked")

    async def should_not_run(*args, **kwargs):
        raise AssertionError("run_all must not be called when the receipt file cannot be reserved")

    monkeypatch.setattr(eval_script.os, "open", fail_open)
    monkeypatch.setattr(eval_script, "run_all", should_not_run)

    assert eval_script.main(["--fixtures", str(_fixture_file(tmp_path)), "--output-dir", str(output_dir)]) == 2
    assert output_dir.is_dir()
    assert "cannot create receipt file" in capsys.readouterr().err


def test_successful_run_creates_private_output_dir_and_receipt_file(monkeypatch, tmp_path) -> None:
    output_dir = tmp_path / "evals"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "load_config", lambda path: _config(models=["only"]))

    async def fake_run_all(models, _fixtures, **kwargs):
        return [
            {
                "schema_version": eval_script.RECEIPT_SCHEMA_VERSION,
                "run_id": kwargs["run_id"],
                "model": models[0],
                "fixture_id": "one",
                "json_ok": True,
                "floor": {"status": "PASS", "gates": {}},
                "latency_ms": 1,
                "output_chars": 2,
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "cost_usd": 0.01,
                "error": None,
            }
        ]

    monkeypatch.setattr(eval_script, "run_all", fake_run_all)

    assert eval_script.main(["--fixtures", str(_fixture_file(tmp_path)), "--output-dir", str(output_dir)]) == 0

    receipt_files = list(output_dir.glob("eval-openai-script-model-*.jsonl"))
    assert len(receipt_files) == 1
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt_files[0].stat().st_mode) == 0o600
    assert json.loads(receipt_files[0].read_text(encoding="utf-8"))["fixture_id"] == "one"


def test_dry_run_reports_cost_bounds_without_key_run_or_output(monkeypatch, tmp_path, capsys) -> None:
    fixtures = _fixture_file(tmp_path)
    output_dir = tmp_path / "evals"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(eval_script, "load_config", lambda path: _config(models=["first", "second"]))

    async def should_not_run(*args, **kwargs):
        raise AssertionError("dry-run must not call the provider runner")

    monkeypatch.setattr(eval_script, "run_all", should_not_run)

    assert eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(output_dir), "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "EVAL PREFLIGHT (no provider calls)" in output
    assert "logical cases: 2" in output
    assert "maximum provider requests: 8" in output
    assert "absolute HTTP-attempt ceiling" in output
    assert "maximum completion tokens (base + one retry per case): 2598" in output
    assert not output_dir.exists()


def test_preflight_includes_reasoning_headroom_and_retry_ceiling() -> None:
    details = eval_script.preflight(
        ["candidate"],
        [{"id": "one", "caller": "banter", "max_tokens": 100, "prompt": "p"}],
        config=_config(),
    )

    assert details["logical_case_count"] == 1
    assert details["max_provider_request_count"] == 4
    assert details["max_completion_tokens_per_model"] == (100 + 512) + (round(100 * 1.75) + 512)
    assert details["max_completion_tokens_total"] == 1299


def test_unpriced_model_requires_explicit_acknowledgement(monkeypatch, tmp_path, capsys) -> None:
    fixtures = _fixture_file(tmp_path)
    output_dir = tmp_path / "evals"
    config = _config(unpriced=True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "load_config", lambda path: config)

    assert (
        eval_script.main(
            ["--fixtures", str(fixtures), "--output-dir", str(output_dir), "--models", "experimental-model"]
        )
        == 2
    )
    assert "--allow-unpriced" in capsys.readouterr().err
    assert not output_dir.exists()


def test_allow_unpriced_permits_an_explicit_real_run(monkeypatch, tmp_path) -> None:
    fixtures = _fixture_file(tmp_path)
    config = _config(unpriced=True)
    seen: list[list[str]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "load_config", lambda path: config)

    async def fake_run_all(models, _fixtures, **kwargs):
        seen.append(models)
        return []

    monkeypatch.setattr(eval_script, "run_all", fake_run_all)

    assert (
        eval_script.main(
            [
                "--fixtures",
                str(fixtures),
                "--output-dir",
                str(tmp_path / "evals"),
                "--models",
                "experimental-model",
                "--allow-unpriced",
            ]
        )
        == 0
    )
    assert seen == [["experimental-model"]]


def test_eval_harness_uses_registry_defaults_unless_models_are_explicit(monkeypatch, tmp_path) -> None:
    fixtures = _fixture_file(tmp_path)
    seen: list[tuple[list[str], dict]] = []
    config = _config(models=["registry-default"])

    async def fake_run_all(models, _fixtures, **kwargs):
        seen.append((models, kwargs))
        return []

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(eval_script, "load_config", lambda path: config)
    monkeypatch.setattr(eval_script, "run_all", fake_run_all)

    assert eval_script.main(["--fixtures", str(fixtures), "--output-dir", str(tmp_path / "registry")]) == 0
    assert seen[0][0] == ["registry-default"]
    assert seen[0][1]["config"] is config

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
    assert seen[1][0] == ["explicit-candidate"]


@pytest.mark.asyncio
async def test_run_one_emits_schema_versioned_floor_receipt(monkeypatch) -> None:
    config = _config()

    async def fake_generate(**kwargs):
        return {"lines": [{"host": "Marco", "text": "Ciao da Radio Kiss Kiss."}]}

    monkeypatch.setattr(eval_script, "_generate_json_response", fake_generate)
    state = SimpleNamespace(_eval_last_prompt_tokens=12, _eval_last_completion_tokens=8)
    record = await eval_script.run_one(
        model="registry-large",
        fixture={"id": "one", "caller": "banter", "max_tokens": 100, "prompt": "p"},
        config=config,
        state=state,
        run_id="run-1",
    )

    assert record["schema_version"] == 1
    assert record["run_id"] == "run-1"
    assert record["result_status"] == "evaluated"
    assert record["floor"]["status"] == "FAIL"
    assert record["floor"]["gates"]["station_name"]["reason"] == "foreign_station_name"


@pytest.mark.asyncio
async def test_run_one_keeps_generation_errors_out_of_floor_results(monkeypatch) -> None:
    config = _config()

    async def fake_generate(**kwargs):
        raise ValueError("not JSON")

    monkeypatch.setattr(eval_script, "_generate_json_response", fake_generate)
    state = SimpleNamespace(_eval_last_prompt_tokens=0, _eval_last_completion_tokens=0)
    record = await eval_script.run_one(
        model="registry-large",
        fixture={"id": "one", "caller": "banter", "max_tokens": 100, "prompt": "p"},
        config=config,
        state=state,
        run_id="run-1",
    )

    assert record["json_ok"] is False
    assert record["result_status"] == "generation_error"
    assert record["floor"] is None


def test_summary_keeps_floor_and_generation_failures_separate() -> None:
    summary = eval_script.summarize(
        [
            {
                "model": "model-a",
                "fixture_id": "bad-floor",
                "json_ok": True,
                "floor": {
                    "status": "FAIL",
                    "gates": {"station_name": {"status": "FAIL", "reason": "foreign_station_name"}},
                },
                "latency_ms": 10,
                "output_chars": 3,
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "cost_usd": 0.1,
                "error": None,
            },
            {
                "model": "model-a",
                "fixture_id": "broken-json",
                "json_ok": False,
                "floor": None,
                "result_status": "generation_error",
                "latency_ms": 20,
                "output_chars": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "error": "ValueError: not JSON",
            },
        ]
    )

    assert "Floor failures:" in summary
    assert "bad-floor: foreign_station_name" in summary
    assert "Provider/JSON failures:" in summary
    assert "broken-json: ValueError: not JSON" in summary


def test_default_banter_corpus_has_no_stale_sofia_prompt() -> None:
    fixtures = eval_script.load_fixtures(eval_script.DEFAULT_FIXTURES)

    assert all("Sofia" not in fixture["prompt"] for fixture in fixtures if fixture["caller"] == "banter")
