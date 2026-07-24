"""Regression tests for station-level listener epochs."""

from __future__ import annotations

import pytest

from mammamiradio.core.listener_session import (
    COMPANIONSHIP_MIN_ACTIVE_SECONDS,
    LISTENER_SESSION_GAP_SECONDS,
    CompanionshipDurationBucket,
    CompanionshipPromptContext,
    ListenerSession,
    ListenerSessionCueState,
    ListenerSessionTransitionKind,
    companionship_duration_bucket,
)


def test_listener_session_exposes_the_fixed_companionship_threshold():
    assert ListenerSession.COMPANIONSHIP_MIN_ACTIVE_SECONDS == 1800.0


def test_first_start_and_concurrent_listener_churn_share_one_epoch():
    session = ListenerSession()

    started = session.observe_active_count(1, now=0.0)
    assert started is not None
    assert started.kind is ListenerSessionTransitionKind.STARTED
    assert started.epoch == 1
    assert session.pending_persona_epochs == (1,)

    assert session.observe_active_count(2, now=1.0).kind is ListenerSessionTransitionKind.ACTIVE_COUNT_CHANGED
    assert session.observe_active_count(3, now=2.0).kind is ListenerSessionTransitionKind.ACTIVE_COUNT_CHANGED
    assert session.epoch == 1

    session.observe_active_count(2, now=3.0)
    session.observe_active_count(1, now=4.0)
    assert session.epoch == 1
    assert session.snapshot(now=4.0).active_count == 1


def test_partial_and_final_disconnect_account_active_time_once():
    session = ListenerSession()
    session.observe_active_count(2, now=10.0)
    session.observe_active_count(1, now=25.0)
    assert session.snapshot(now=30.0).accumulated_active_seconds == pytest.approx(20.0)

    ended = session.observe_active_count(0, now=40.0)
    assert ended is not None
    assert ended.kind is ListenerSessionTransitionKind.BECAME_EMPTY
    assert session.snapshot(now=40.0).accumulated_active_seconds == pytest.approx(30.0)


def test_resume_just_before_gap_keeps_epoch_and_exact_gap_starts_next():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    session.observe_active_count(0, now=1.0)

    resumed = session.observe_active_count(1, now=1.0 + LISTENER_SESSION_GAP_SECONDS - 0.001)
    assert resumed is not None
    assert resumed.kind is ListenerSessionTransitionKind.RESUMED
    assert resumed.epoch == 1

    session.observe_active_count(0, now=2.0)
    restarted = session.observe_active_count(1, now=2.0 + LISTENER_SESSION_GAP_SECONDS)
    assert restarted is not None
    assert restarted.kind is ListenerSessionTransitionKind.STARTED
    assert restarted.epoch == 2
    assert session.pending_persona_epochs == (1, 2)


def test_persona_receipt_acknowledgement_is_idempotent():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    assert session.mark_persona_recorded(1) is True
    assert session.mark_persona_recorded(1) is False
    assert session.pending_persona_epochs == ()


def test_process_reset_starts_a_fresh_epoch():
    first_process = ListenerSession()
    first_process.observe_active_count(1, now=0.0)
    second_process = ListenerSession()
    started = second_process.observe_active_count(1, now=0.0)
    assert started is not None
    assert started.epoch == 1


def test_exact_600_second_gap_starts_new_epoch_but_shorter_gap_resumes():
    session = ListenerSession()
    session.observe_active_count(1, now=10.0)
    session.observe_active_count(0, now=20.0)

    resumed = session.observe_active_count(1, now=20.0 + LISTENER_SESSION_GAP_SECONDS - 0.000_001)
    assert resumed is not None
    assert resumed.kind is ListenerSessionTransitionKind.RESUMED
    session.observe_active_count(0, now=700.0)

    restarted = session.observe_active_count(1, now=700.0 + LISTENER_SESSION_GAP_SECONDS)
    assert restarted is not None
    assert restarted.kind is ListenerSessionTransitionKind.STARTED
    assert restarted.epoch == 2


def test_active_time_accumulates_across_grace_without_counting_empty_time():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    session.observe_active_count(0, now=1_000.0)
    session.observe_active_count(1, now=1_500.0)

    assert session.snapshot(now=2_300.0).accumulated_active_seconds == pytest.approx(1_800.0)
    assert session.snapshot(now=2_300.0).companionship_eligible is True


def test_seventeen_connects_and_sixteen_disconnects_do_not_split_epoch():
    session = ListenerSession()
    for count in range(1, 18):
        session.observe_active_count(count, now=float(count))
    for offset, count in enumerate(range(16, 0, -1), start=18):
        session.observe_active_count(count, now=float(offset))

    snapshot = session.snapshot(now=40.0)
    assert snapshot.epoch == 1
    assert snapshot.active_count == 1
    assert session.pending_persona_epochs == (1,)


def test_companionship_threshold_claim_queue_and_consume_are_one_shot():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)

    assert session.claim_companionship(now=COMPANIONSHIP_MIN_ACTIVE_SECONDS - 0.001) is None
    assert session.companionship_cue_state is ListenerSessionCueState.UNAVAILABLE
    assert (
        session.refresh_companionship_availability(now=COMPANIONSHIP_MIN_ACTIVE_SECONDS)
        is ListenerSessionCueState.AVAILABLE
    )

    claim = session.claim_companionship(now=COMPANIONSHIP_MIN_ACTIVE_SECONDS)
    assert claim is not None
    assert claim.epoch == 1
    assert claim.prompt_context.duration_bucket is CompanionshipDurationBucket.MINUTES_30_TO_44
    assert session.companionship_cue_state is ListenerSessionCueState.ATTEMPTED
    assert session.claim_companionship(now=COMPANIONSHIP_MIN_ACTIVE_SECONDS + 1) is None

    assert session.mark_companionship_queued(1) is True
    assert session.companionship_cue_state is ListenerSessionCueState.QUEUED
    assert session.mark_companionship_consumed(1) is True
    assert session.companionship_cue_state is ListenerSessionCueState.CONSUMED
    assert session.abandon_companionship(1) is False
    assert session.claim_companionship(now=10_000.0) is None


def test_failed_companionship_attempt_is_permanently_abandoned_for_epoch():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    claim = session.claim_companionship(now=1_800.0)
    assert claim is not None

    assert session.abandon_companionship(claim.epoch) is True
    assert session.abandon_companionship(claim.epoch) is False
    assert session.companionship_cue_state is ListenerSessionCueState.ABANDONED
    assert session.claim_companionship(now=7_200.0) is None


def test_companionship_is_unavailable_while_empty_and_resumes_same_epoch():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    session.observe_active_count(0, now=1_800.0)

    empty = session.snapshot(now=2_000.0)
    assert empty.companionship_eligible is False
    assert empty.companionship_cue_state is ListenerSessionCueState.UNAVAILABLE
    assert session.claim_companionship(now=2_000.0) is None

    session.observe_active_count(1, now=2_399.999)
    claim = session.claim_companionship(now=2_399.999)
    assert claim is not None
    assert claim.epoch == 1


def test_new_epoch_resets_terminal_companionship_state():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    claim = session.claim_companionship(now=1_800.0)
    assert claim is not None
    assert session.abandon_companionship(1) is True
    session.observe_active_count(0, now=1_801.0)

    session.observe_active_count(1, now=1_801.0 + LISTENER_SESSION_GAP_SECONDS)
    assert session.epoch == 2
    assert session.companionship_cue_state is ListenerSessionCueState.UNAVAILABLE
    assert session.claim_companionship(now=4_201.0) is not None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (1_799.999, None),
        (1_800.0, CompanionshipDurationBucket.MINUTES_30_TO_44),
        (2_700.0, CompanionshipDurationBucket.MINUTES_45_TO_59),
        (3_600.0, CompanionshipDurationBucket.MINUTES_60_TO_89),
        (5_400.0, CompanionshipDurationBucket.MINUTES_90_PLUS),
    ],
)
def test_companionship_duration_buckets_are_coarse(seconds, expected):
    assert companionship_duration_bucket(seconds) is expected


def test_prompt_context_and_admin_diagnostics_expose_no_receipt_or_exact_threshold():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    claim = session.claim_companionship(now=1_800.0)
    assert claim is not None

    prompt = claim.prompt_context.to_prompt_context()
    assert "listener-epoch" not in prompt
    assert "1800" not in prompt
    assert "1 listener" not in prompt
    diagnostics = session.snapshot(now=1_800.0).to_dict()
    assert diagnostics["persona_pending"] is True
    assert diagnostics["persona_pending_count"] == 1
    assert "pending_persona_epochs" not in diagnostics


@pytest.mark.parametrize(
    ("bucket", "copy"),
    [
        (CompanionshipDurationBucket.MINUTES_30_TO_44, "We've had company for roughly half an hour."),
        (CompanionshipDurationBucket.MINUTES_45_TO_59, "Siamo insieme da quasi un'ora."),
        (CompanionshipDurationBucket.MINUTES_60_TO_89, "This shared listening has lasted over an hour."),
        (CompanionshipDurationBucket.MINUTES_90_PLUS, "Compagnia da oltre un'ora e mezza."),
    ],
)
def test_companionship_copy_proof_requires_application_owned_context_markers(bucket, copy):
    context = CompanionshipPromptContext(bucket)
    assert context.is_used_by(copy) is True
    assert context.is_used_by("The studio keeps moving and the next record is ready.") is False
    assert context.is_used_by("It is good to have company tonight.") is False


@pytest.mark.parametrize(
    ("claimed", "copy"),
    [
        (
            CompanionshipDurationBucket.MINUTES_60_TO_89,
            "We've had company for over an hour and a half.",
        ),
        (
            CompanionshipDurationBucket.MINUTES_45_TO_59,
            "Siamo insieme da quasi un'ora e mezza.",
        ),
    ],
)
def test_companionship_copy_proof_rejects_conflicting_longer_duration_bucket(claimed, copy):
    assert CompanionshipPromptContext(claimed).is_used_by(copy) is False


def test_stale_epoch_cannot_queue_consume_or_abandon_current_cue():
    session = ListenerSession()
    session.observe_active_count(1, now=0.0)
    assert session.claim_companionship(now=1_800.0) is not None

    assert session.mark_companionship_queued(2) is False
    assert session.mark_companionship_consumed(2) is False
    assert session.abandon_companionship(2) is False
    assert session.companionship_cue_state is ListenerSessionCueState.ATTEMPTED
