from __future__ import annotations

import json

from mammamiradio.release_campaign import (
    ACTIVE,
    AIRED_ATTEMPT,
    LEDGER_FILENAME,
    QUEUED_ATTEMPT,
    RETIRED,
    ReleaseBeatManifest,
    ReleaseCampaign,
    ReleaseCampaignLedger,
    load_release_beat_manifest,
)

BASE = 1_780_000_000.0


def manifest(**overrides) -> ReleaseBeatManifest:
    data = ReleaseBeatManifest(
        enabled=True,
        id="edge-4a15270-hans-guenther",
        channel="edge",
        build_sha="4a1527080692eed5541e72a5a2b0f2c344e3ca9a",
        facts=("Guest host can be turned on or off", "Hans Guenther is the guest host"),
        props=("human-sized crate", "brass music leaking out"),
        max_airings=2,
        campaign_window_seconds=72 * 60 * 60,
        min_seconds_between_airings=45 * 60,
        min_segments_between_airings=6,
    ).to_prompt_payload()
    data.update(
        {
            "enabled": True,
            "max_airings": 2,
            "campaign_window_seconds": 72 * 60 * 60,
            "min_seconds_between_airings": 45 * 60,
            "min_segments_between_airings": 6,
        }
    )
    data.update(overrides)
    return ReleaseBeatManifest.from_dict(data)


def campaign(tmp_path, *, beat: ReleaseBeatManifest | None = None) -> ReleaseCampaign:
    return ReleaseCampaign(tmp_path, manifest=beat or manifest(), clock=lambda: BASE)


def test_missing_manifest_is_disabled(tmp_path):
    beat = load_release_beat_manifest(tmp_path / "missing.toml")
    assert beat.enabled is False
    assert beat.active is False


def test_manifest_loads_flat_or_nested_toml(tmp_path):
    path = tmp_path / "release_beat.toml"
    path.write_text(
        """
[release_beat]
enabled = true
id = "edge-abc123-hans"
channel = "edge"
build_sha = "abc123"
facts = ["Guest host option shipped"]
props = ["crate"]
max_airings = 3
""".strip()
    )
    beat = load_release_beat_manifest(path)
    assert beat.active is True
    assert beat.id == "edge-abc123-hans"
    assert beat.facts == ("Guest host option shipped",)
    assert beat.max_airings == 3


def test_manifest_accepts_validator_copy_and_avoid_aliases(tmp_path):
    path = tmp_path / "release_beat.toml"
    path.write_text(
        """
[release_beat]
enabled = true
id = "edge-abc123-hans"
channel = "edge"
build_sha = "abc123"
facts = ["Guest host option shipped"]
props = ["crate", "brass band"]
copy = ["keep it in-world", "make it sound like studio gossip"]
avoid = ["software update", "changelog"]
""".strip()
    )
    beat = load_release_beat_manifest(path)
    assert beat.copy_guidance == "keep it in-world; make it sound like studio gossip"
    assert beat.forbidden_terms == ("software update", "changelog")


def test_corrupt_ledger_starts_fresh(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text("{not json")
    led = ReleaseCampaignLedger.load(tmp_path, beat_id="beat-1")
    assert led.beat_id == "beat-1"
    assert led.status == ACTIVE
    assert led._dirty is True


def test_new_manifest_id_resets_old_ledger(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps({"beat_id": "old", "status": RETIRED}))
    led = ReleaseCampaignLedger.load(tmp_path, beat_id="new")
    assert led.beat_id == "new"
    assert led.status == ACTIVE


def test_first_attempt_moves_to_queued_and_prompt_rotates_copy(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    assert camp.ledger.status == QUEUED_ATTEMPT
    assert camp.ledger.attempt_id == offer.attempt_id
    assert offer.prompt_payload["facts"][0] == "Guest host can be turned on or off"


def test_model_ignored_release_beat_restores_active(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=False)
    assert camp.ledger.status == ACTIVE
    assert camp.ledger.attempt_id == ""


def test_model_used_release_beat_waits_for_stream_delivery(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True, queue_id="q1")
    assert camp.ledger.status == AIRED_ATTEMPT
    assert camp.ledger.queued_segment_id == "q1"
    assert camp.ledger.aired_count == 0


def test_stale_inflight_ledger_reactivates_on_load(tmp_path):
    beat = manifest()
    (tmp_path / LEDGER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "beat_id": beat.id,
                "status": AIRED_ATTEMPT,
                "attempt_id": "attempt-1",
                "queued_segment_id": "q1",
            }
        )
    )

    camp = ReleaseCampaign(tmp_path, manifest=beat, clock=lambda: BASE)

    assert camp.ledger.status == ACTIVE
    assert camp.ledger.attempt_id == ""
    assert camp.ledger.queued_segment_id == ""
    assert camp.ledger._dirty is True
    assert camp.is_due(now=BASE) is True


def test_queue_discard_restores_used_beat_before_stream_delivery(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True, queue_id="q1")

    restored = camp.record_queue_discard(
        {
            **offer.segment_metadata(),
            "queue_id": "q1",
        }
    )

    assert restored is True
    assert camp.ledger.status == ACTIVE
    assert camp.ledger.attempt_id == ""
    assert camp.ledger.queued_segment_id == ""
    # Restored to eligible, but the first-airing throttle still governs timing:
    # not re-offered on the very next cycle, available again after min_seconds.
    assert camp.is_due(now=BASE + 1) is False
    assert camp.is_due(now=BASE + 3000) is True


def test_queue_discard_ignores_stale_release_attempt(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True, queue_id="q1")

    restored = camp.record_queue_discard(
        {
            "release_beat_id": offer.beat_id,
            "release_beat_attempt_id": "other-attempt",
            "queue_id": "q1",
        }
    )

    assert restored is False
    assert camp.ledger.status == AIRED_ATTEMPT
    assert camp.ledger.attempt_id == offer.attempt_id


def test_delivery_counts_only_active_listeners_positive_bytes_and_not_skipped(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True, queue_id="q1")

    counted = camp.record_stream_result(
        offer.segment_metadata(),
        bytes_sent=0,
        was_skipped=False,
        listeners=1,
        now=BASE + 10,
    )
    assert counted is False
    assert camp.ledger.aired_count == 0
    assert camp.ledger.status == ACTIVE

    # A non-delivery restores ACTIVE but does not reset the offer clock, so the
    # first-airing throttle (min_seconds) still governs the retry timing.
    assert camp.begin_attempt(now=BASE + 11) is None
    retry = camp.begin_attempt(now=BASE + 3000)
    assert retry is not None
    camp.mark_generation_result(attempt_id=retry.attempt_id, release_beat_used=True, queue_id="q2")
    counted = camp.record_stream_result(
        retry.segment_metadata(),
        bytes_sent=4096,
        was_skipped=False,
        listeners=1,
        now=BASE + 3010,
    )
    assert counted is True
    assert camp.ledger.aired_count == 1
    assert camp.ledger.last_aired_at == BASE + 3010
    assert camp.ledger.status == ACTIVE


def test_repeat_airing_waits_for_time_or_non_release_spacing(tmp_path):
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True)
    camp.record_stream_result(offer.segment_metadata(), bytes_sent=100, was_skipped=False, listeners=1, now=BASE)

    assert camp.is_due(now=BASE + 60) is False
    for i in range(6):
        camp.record_stream_result({}, bytes_sent=100, was_skipped=False, listeners=1, now=BASE + 60 + i)
    assert camp.is_due(now=BASE + 120) is True


def test_budget_retires_campaign(tmp_path):
    camp = campaign(tmp_path)
    for offset in (0, 3600):
        offer = camp.begin_attempt(now=BASE + offset)
        assert offer is not None
        camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True)
        camp.record_stream_result(
            offer.segment_metadata(),
            bytes_sent=100,
            was_skipped=False,
            listeners=1,
            now=BASE + offset,
        )
    assert camp.ledger.status == RETIRED


def test_retired_campaign_stops_dirtying_ledger_on_ordinary_segments(tmp_path):
    """A RETIRED campaign must not keep marking the ledger dirty on every
    ordinary delivered segment — that would mean a synchronous disk write on
    every segment for the rest of the session (adversarial-review finding)."""
    beat = manifest(max_airings=1)
    camp = campaign(tmp_path, beat=beat)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True)
    camp.record_stream_result(offer.segment_metadata(), bytes_sent=100, was_skipped=False, listeners=1, now=BASE)
    assert camp.ledger.status == RETIRED

    camp.save_if_dirty()
    assert camp.ledger._dirty is False

    counted = camp.record_stream_result({}, bytes_sent=100, was_skipped=False, listeners=1, now=BASE + 10)

    assert counted is False
    assert camp.ledger._dirty is False
    assert camp.ledger.non_release_segments_since_last_airing == 0
    assert camp.ledger.retired_reason == "budget_exhausted"
    assert camp.is_due(now=BASE + 7200) is False


def test_window_expiry_retires_campaign(tmp_path):
    beat = manifest(max_airings=5, campaign_window_seconds=60)
    camp = campaign(tmp_path, beat=beat)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True)
    camp.record_stream_result(offer.segment_metadata(), bytes_sent=100, was_skipped=False, listeners=1, now=BASE)

    assert camp.is_due(now=BASE + 61) is False
    assert camp.ledger.status == RETIRED
    assert camp.ledger.retired_reason == "window_expired"


def test_save_if_dirty_persists_atomically(tmp_path):
    camp = campaign(tmp_path)
    camp.ledger.aired_count = 1
    camp.ledger._dirty = True
    camp.save_if_dirty()

    saved = json.loads((tmp_path / LEDGER_FILENAME).read_text())
    assert saved["aired_count"] == 1
    assert camp.ledger._dirty is False


def test_save_if_dirty_is_noop_when_clean(tmp_path):
    """The per-segment synchronous save (streamer hot path) must not touch disk
    unless the ledger actually changed."""
    camp = campaign(tmp_path)
    camp.ledger._dirty = False
    camp.save_if_dirty()
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_never_aired_beat_throttles_offers_and_never_relatches(tmp_path):
    """A declined (never-aired) beat must NOT be re-offered every cycle.

    Regression guard for the music-starvation P0: aired_count stays 0, so the
    throttle must key off last_attempt_at (time), NOT non_release_segments — that
    counter only resets on a real airing and would otherwise latch >= min_segments
    forever, re-forcing banter on every producer iteration.
    """
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=False)
    assert camp.ledger.status == ACTIVE
    assert camp.ledger.aired_count == 0

    # Not re-offered on the very next cycle.
    assert camp.is_due(now=BASE + 1) is False
    # Feed many delivered non-release segments: the min_segments branch must NOT
    # relatch is_due for a never-aired beat.
    for i in range(20):
        camp.record_stream_result({}, bytes_sent=100, was_skipped=False, listeners=1, now=BASE + 1 + i)
    assert camp.is_due(now=BASE + 60) is False
    # Available again only after the time window.
    assert camp.is_due(now=BASE + 2700) is True


def test_never_aired_min_segments_zero_still_throttles(tmp_path):
    """With min_segments_between_airings=0 (a valid config), the never-aired
    branch must still throttle on time — not fall through to an always-true
    segment check that resurrects the starvation loop."""
    camp = campaign(tmp_path, beat=manifest(min_segments_between_airings=0))
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=False)
    assert camp.is_due(now=BASE + 1) is False
    assert camp.is_due(now=BASE + 2700) is True


def test_never_aired_campaign_retires_after_window(tmp_path):
    """A beat the host keeps declining self-retires at the campaign window,
    anchored on first_attempt_at (first_aired_at never gets set)."""
    camp = campaign(tmp_path, beat=manifest(campaign_window_seconds=60))
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    assert camp.ledger.first_attempt_at == BASE
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=False)

    assert camp.is_due(now=BASE + 61) is False
    assert camp.ledger.status == RETIRED
    assert camp.ledger.retired_reason == "window_expired"


def test_first_attempt_at_persists_round_trip(tmp_path):
    camp = campaign(tmp_path)
    camp.begin_attempt(now=BASE)
    assert camp.ledger.first_attempt_at == BASE
    camp.save_if_dirty()

    reloaded = ReleaseCampaignLedger.load(tmp_path, beat_id=camp.manifest.id)
    assert reloaded.first_attempt_at == BASE


def test_abandon_in_flight_reactivates_queued_attempt(tmp_path):
    """F5: a begun-but-never-queued beat (no surviving commit) is restored."""
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    assert camp.ledger.status == QUEUED_ATTEMPT

    camp.abandon_in_flight()

    assert camp.ledger.status == ACTIVE
    assert camp.ledger.attempt_id == ""


def test_abandon_in_flight_does_not_clobber_queued_beat(tmp_path):
    """An already-queued beat (AIRED_ATTEMPT) must still air — abandon_in_flight
    only touches QUEUED_ATTEMPT."""
    camp = campaign(tmp_path)
    offer = camp.begin_attempt(now=BASE)
    assert offer is not None
    camp.mark_generation_result(attempt_id=offer.attempt_id, release_beat_used=True, queue_id="q1")
    assert camp.ledger.status == AIRED_ATTEMPT

    camp.abandon_in_flight()

    assert camp.ledger.status == AIRED_ATTEMPT
    assert camp.ledger.queued_segment_id == "q1"
