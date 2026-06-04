from __future__ import annotations

import json

import pytest

from scripts import eval_tts_ha_green_viability as eval_tts

REQUIRED_RECORD_FIELDS = {
    "provider",
    "kind",
    "ha_green_status",
    "reason",
    "required_env",
    "required_packages",
    "aarch64_image_risk",
    "runtime_risk",
    "expected_latency",
    "operator_cost",
}

PASS_STATUSES = {eval_tts.STATUS_PASS}
CLOUD_KINDS = {eval_tts.KIND_CLOUD}
LOCAL_CPU_KINDS = {eval_tts.KIND_LOCAL_CPU}
LOCAL_GPU_REJECT_KINDS = {eval_tts.KIND_REJECTED_LOCAL}


def _by_provider(records: list[dict]) -> dict[str, dict]:
    return {record["provider"]: record for record in records}


def test_default_provider_matrix_covers_expected_ha_green_candidates() -> None:
    records = eval_tts.build_provider_matrix()

    assert [record["provider"] for record in records] == eval_tts.DEFAULT_PROVIDERS
    assert len(records) == len(set(eval_tts.DEFAULT_PROVIDERS))
    for record in records:
        assert set(record) == REQUIRED_RECORD_FIELDS
        assert isinstance(record["required_env"], list)
        assert isinstance(record["required_packages"], list)

    by_provider = _by_provider(records)
    assert {"edge", "openai", "azure", "elevenlabs", "kokoro", "piper", "f5tts", "bark_suno"} <= set(by_provider)


def test_edge_and_openai_classification_matches_current_tts_strategy() -> None:
    by_provider = _by_provider(eval_tts.build_provider_matrix(["edge", "openai"]))

    edge = by_provider["edge"]
    assert edge["kind"] in CLOUD_KINDS
    assert edge["ha_green_status"] in PASS_STATUSES
    assert edge["required_env"] == []
    assert "edge-tts" in edge["required_packages"]
    assert "low" in edge["aarch64_image_risk"]
    assert "free" in edge["operator_cost"].lower()

    openai = by_provider["openai"]
    assert openai["kind"] in CLOUD_KINDS
    assert openai["ha_green_status"] == eval_tts.STATUS_CONDITIONAL
    assert openai["required_env"] == ["OPENAI_API_KEY"]
    assert "openai" in openai["required_packages"]
    assert "low" in openai["aarch64_image_risk"]
    assert "key" in openai["reason"].lower()


def test_optional_cloud_clients_are_conditional_not_default_dependencies() -> None:
    by_provider = _by_provider(eval_tts.build_provider_matrix(["azure", "elevenlabs"]))

    assert by_provider["azure"]["kind"] in CLOUD_KINDS
    assert by_provider["azure"]["ha_green_status"] == eval_tts.STATUS_CONDITIONAL
    assert by_provider["azure"]["required_env"] == ["AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"]
    assert by_provider["azure"]["required_packages"]

    assert by_provider["elevenlabs"]["kind"] in CLOUD_KINDS
    assert by_provider["elevenlabs"]["ha_green_status"] == eval_tts.STATUS_CONDITIONAL
    assert by_provider["elevenlabs"]["required_env"] == ["ELEVENLABS_API_KEY"]
    assert by_provider["elevenlabs"]["required_packages"]


def test_local_cpu_candidates_are_conditional_on_aarch64_packages_and_latency() -> None:
    by_provider = _by_provider(eval_tts.build_provider_matrix(["kokoro", "piper"]))

    for provider in ("kokoro", "piper"):
        record = by_provider[provider]
        assert record["kind"] in LOCAL_CPU_KINDS
        assert record["ha_green_status"] == eval_tts.STATUS_CONDITIONAL
        assert record["required_packages"]
        assert any(risk in record["aarch64_image_risk"] for risk in ("medium", "high"))
        assert "cpu" in record["runtime_risk"].lower()
        assert record["expected_latency"]


def test_gpu_or_heavy_local_models_fail_by_default_on_ha_green() -> None:
    by_provider = _by_provider(eval_tts.build_provider_matrix(["f5-tts", "bark"]))

    assert by_provider["f5tts"]["kind"] in LOCAL_GPU_REJECT_KINDS
    assert by_provider["f5tts"]["ha_green_status"] == eval_tts.STATUS_FAIL
    assert "gpu" in by_provider["f5tts"]["reason"].lower()

    assert by_provider["bark_suno"]["kind"] in LOCAL_GPU_REJECT_KINDS
    assert by_provider["bark_suno"]["ha_green_status"] == eval_tts.STATUS_FAIL
    assert "slow" in by_provider["bark_suno"]["reason"].lower()


def test_provider_filtering_preserves_requested_order_and_rejects_unknown_provider() -> None:
    records = eval_tts.build_provider_matrix(["piper", "edge"])
    assert [record["provider"] for record in records] == ["piper", "edge"]

    with pytest.raises(ValueError, match=r"(?i)unsupported provider.*not_a_provider"):
        eval_tts.build_provider_matrix(["edge", "not-a-provider"])


def test_render_markdown_is_deterministic_for_operator_review() -> None:
    records = [
        {
            "provider": "edge",
            "kind": "cloud",
            "ha_green_status": "viable",
            "reason": "Bundled dependency path already supports it.",
            "required_env": [],
            "required_packages": ["edge-tts"],
            "aarch64_image_risk": "low",
            "runtime_risk": "low",
            "expected_latency": "sub-second",
            "operator_cost": "free",
        },
        {
            "provider": "openai",
            "kind": "cloud",
            "ha_green_status": "conditional",
            "reason": "Requires an API key and network.",
            "required_env": ["OPENAI_API_KEY"],
            "required_packages": ["openai"],
            "aarch64_image_risk": "low",
            "runtime_risk": "cloud dependency",
            "expected_latency": "1-3s",
            "operator_cost": "metered",
        },
        {
            "provider": "pipe_newline",
            "kind": "cloud",
            "ha_green_status": "conditional",
            "reason": "Needs REST | SDK choice.\nKeep table intact.",
            "required_env": ["PIPE_TEST_KEY"],
            "required_packages": ["httpx | sdk"],
            "aarch64_image_risk": "low | medium",
            "runtime_risk": "network\nquota",
            "expected_latency": "1-3s",
            "operator_cost": "metered",
        },
    ]

    first_render = eval_tts.render_markdown(records)
    second_render = eval_tts.render_markdown(records)

    assert first_render == second_render
    assert first_render.startswith("# HA Green TTS Viability")
    assert "| Provider | Kind | HA Green status |" in first_render
    assert "| edge | cloud | viable |" in first_render
    assert "OPENAI_API_KEY" in first_render
    assert "Requires an API key and network." in first_render
    assert "Needs REST \\| SDK choice. Keep table intact." in first_render
    assert "httpx \\| sdk" in first_render
    assert first_render.count("| pipe_newline |") == 1


def test_write_reports_emits_deterministic_markdown_and_jsonl(tmp_path) -> None:
    records = [
        {
            "provider": "edge",
            "kind": "cloud",
            "ha_green_status": "viable",
            "reason": "ok",
            "required_env": [],
            "required_packages": ["edge-tts"],
            "aarch64_image_risk": "low",
            "runtime_risk": "low",
            "expected_latency": "sub-second",
            "operator_cost": "free",
        },
        {
            "provider": "piper",
            "kind": "local_cpu",
            "ha_green_status": "conditional",
            "reason": "Needs a voice model.",
            "required_env": [],
            "required_packages": ["piper-tts"],
            "aarch64_image_risk": "medium",
            "runtime_risk": "cpu budget",
            "expected_latency": "sub-second",
            "operator_cost": "free",
        },
    ]

    paths = eval_tts.write_reports(records, tmp_path, timestamp="20260519T120000Z")

    assert paths == {
        "markdown": tmp_path / "tts-ha-green-viability-20260519T120000Z.md",
        "jsonl": tmp_path / "tts-ha-green-viability-20260519T120000Z.jsonl",
    }
    assert paths["markdown"].read_text() == eval_tts.render_markdown(records)
    assert paths["jsonl"].read_text().splitlines() == [
        json.dumps(records[0], sort_keys=True),
        json.dumps(records[1], sort_keys=True),
    ]


def test_write_reports_rejects_path_like_timestamp(tmp_path) -> None:
    records = eval_tts.build_provider_matrix(["edge"])

    with pytest.raises(ValueError, match="YYYYMMDDTHHMMSSZ"):
        eval_tts.write_reports(records, tmp_path, timestamp="../bad")

    assert list(tmp_path.iterdir()) == []


def test_write_reports_refuses_to_overwrite_existing_report(tmp_path) -> None:
    records = eval_tts.build_provider_matrix(["edge"])
    eval_tts.write_reports(records, tmp_path, timestamp="20260519T120000Z")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        eval_tts.write_reports(records, tmp_path, timestamp="20260519T120000Z")


def test_cli_provider_filtering_writes_only_requested_records(tmp_path, capsys) -> None:
    rc = eval_tts.main(
        [
            "--providers",
            "edge",
            "openai",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "20260519T120000Z",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "tts-ha-green-viability-20260519T120000Z.md" in captured.out

    jsonl_path = tmp_path / "tts-ha-green-viability-20260519T120000Z.jsonl"
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert [record["provider"] for record in records] == ["edge", "openai"]


def test_cli_reports_unsupported_provider_without_writing_reports(tmp_path, capsys) -> None:
    rc = eval_tts.main(
        [
            "--providers",
            "edge",
            "not-a-provider",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "20260519T120000Z",
        ]
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "unsupported provider" in captured.err.lower()
    assert "not_a_provider" in captured.err
    assert list(tmp_path.iterdir()) == []


def test_cli_reports_invalid_timestamp_without_writing_reports(tmp_path, capsys) -> None:
    rc = eval_tts.main(
        [
            "--providers",
            "edge",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "../bad",
        ]
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "YYYYMMDDTHHMMSSZ" in captured.err
    assert list(tmp_path.iterdir()) == []


def test_cli_reports_existing_report_without_overwriting(tmp_path, capsys) -> None:
    eval_tts.write_reports(eval_tts.build_provider_matrix(["edge"]), tmp_path, timestamp="20260519T120000Z")
    jsonl_path = tmp_path / "tts-ha-green-viability-20260519T120000Z.jsonl"
    original_jsonl = jsonl_path.read_text()

    rc = eval_tts.main(
        [
            "--providers",
            "edge",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "20260519T120000Z",
        ]
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "Refusing to overwrite" in captured.err
    assert jsonl_path.read_text() == original_jsonl


def test_cli_reports_write_os_error_without_traceback(tmp_path, capsys, monkeypatch) -> None:
    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(eval_tts, "write_reports", fail_write)

    rc = eval_tts.main(["--providers", "edge", "--output-dir", str(tmp_path)])

    assert rc == 2
    captured = capsys.readouterr()
    assert "disk full" in captured.err
