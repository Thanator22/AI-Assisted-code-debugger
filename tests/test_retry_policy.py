import pytest

from repo_doctor.repair import RetryPolicy
from repo_doctor.tools.build import TestRunOutcome as Outcome


def test_default_policy_disallows_retry():
    assert not RetryPolicy().allows_retry(
        attempts_completed=1,
        verification_outcome=Outcome.TESTS_FAILED,
    )


def test_two_attempt_budget_allows_one_retry():
    policy = RetryPolicy(max_attempts=2)

    assert policy.allows_retry(
        attempts_completed=1,
        verification_outcome=Outcome.TESTS_FAILED,
    )

    assert not policy.allows_retry(
        attempts_completed=2,
        verification_outcome=Outcome.TESTS_FAILED,
    )


@pytest.mark.parametrize(
    "outcome",
    [
        outcome
        for outcome in Outcome
        if outcome is not Outcome.TESTS_FAILED
    ],
)
def test_other_outcomes_never_allow_retry(outcome):
    assert not RetryPolicy(max_attempts=3).allows_retry(
        attempts_completed=1,
        verification_outcome=outcome,
    )


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2", None])
def test_invalid_attempt_budget_is_rejected(value):
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", None])
def test_invalid_completed_count_is_rejected(value):
    with pytest.raises(ValueError, match="attempts_completed"):
        RetryPolicy(max_attempts=2).allows_retry(
            attempts_completed=value,
            verification_outcome=Outcome.TESTS_FAILED,
        )
        
from dataclasses import FrozenInstanceError

from repo_doctor.repair import (
    RepairAttemptRecord,
    RepairAttemptStatus,
)


def test_attempt_record_can_capture_proposal_failure():
    record = RepairAttemptRecord(
        number=1,
        status=RepairAttemptStatus.PROPOSAL_FAILED,
        diagnostics=("Model returned an invalid proposal",),
    )

    assert record.patch is None
    assert record.verification is None
    assert record.diagnostics == (
        "Model returned an invalid proposal",
    )


def test_attempt_record_is_immutable():
    record = RepairAttemptRecord(
        number=1,
        status=RepairAttemptStatus.VERIFICATION_FAILED,
    )

    with pytest.raises(FrozenInstanceError):
        record.number = 2


@pytest.mark.parametrize("number", [0, -1, True, 1.5, "1", None])
def test_invalid_attempt_number_is_rejected(number):
    with pytest.raises(ValueError, match="Attempt number"):
        RepairAttemptRecord(
            number=number,
            status=RepairAttemptStatus.PROPOSAL_FAILED,
        )