import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repo_doctor.repair import RepairAttemptStatus, RetryPolicy
from repo_doctor.benchmark import benchmark_fixture

from repo_doctor.backend_selection import Backend, ModelSelection
from repo_doctor.benchmark import BenchmarkRow, run_benchmark


def test_records_failure_and_continues(tmp_path, monkeypatch):
    fixtures = (
        tmp_path / "first",
        tmp_path / "second",
    )
    calls = []

    def fake_attempt(fixture, selection, transport, *, retry_policy):
        calls.append(fixture)
        assert retry_policy.max_attempts == 1

        return BenchmarkRow(
            fixture=str(fixture),
            outcome=("runner_error" if fixture == fixtures[0] else "verified"),
            duration_seconds=0.5,
            workspace=None,
            report_path=None,
            attempt_count=None if fixture == fixtures[0] else 1,
        )

    monkeypatch.setattr(
        "repo_doctor.benchmark.benchmark_fixture",
        fake_attempt,
    )

    summary_path = run_benchmark(
        fixtures=fixtures,
        selection=ModelSelection(Backend.CODEX, "test-model"),
        transport=lambda prompt: "",
        output_root=tmp_path / "results",
        write=lambda message: None,
    )
    payload = json.loads(summary_path.read_text(encoding="utf-8"))

    assert calls == list(fixtures)
    assert payload["max_attempts"] == 1
    assert payload["first_attempt_verified_count"] == 1
    assert payload["results"][0]["attempt_count"] is None
    assert payload["complete"] is True
    assert payload["completed_count"] == 2
    assert payload["verified_count"] == 1
    assert payload["verified_fraction_of_planned"] == 0.5
    assert [row["outcome"] for row in payload["results"]] == [
        "runner_error",
        "verified",
    ]


def test_interruption_preserves_completed_results(tmp_path, monkeypatch):
    import pytest

    fixtures = (Path("first"), Path("second"))

    def fake_attempt(fixture, selection, transport, *, retry_policy):
        if fixture == fixtures[1]:
            raise KeyboardInterrupt()

        return BenchmarkRow(
            fixture=str(fixture),
            outcome="verified",
            duration_seconds=0.5,
            workspace=None,
            report_path=None,
            attempt_count=1,
        )

    monkeypatch.setattr(
        "repo_doctor.benchmark.benchmark_fixture",
        fake_attempt,
    )

    with pytest.raises(KeyboardInterrupt):
        run_benchmark(
            fixtures=fixtures,
            selection=ModelSelection(Backend.CODEX, "test-model"),
            transport=lambda prompt: "",
            output_root=tmp_path,
            write=lambda message: None,
        )

    summary_path = next(tmp_path.glob("*/summary.json"))
    payload = json.loads(summary_path.read_text(encoding="utf-8"))

    assert payload["complete"] is False
    assert payload["planned_count"] == 2
    assert payload["completed_count"] == 1


def test_summary_distinguishes_first_attempt_success(tmp_path, monkeypatch):
    fixtures = tuple(tmp_path / name for name in ("first", "retried", "failed"))
    policy = RetryPolicy(max_attempts=2)

    def fake_fixture(fixture, selection, transport, *, retry_policy):
        assert retry_policy is policy
        index = fixtures.index(fixture)
        return BenchmarkRow(
            fixture=str(fixture),
            outcome="verified" if index < 2 else "verification_failed",
            duration_seconds=0.1,
            workspace=None,
            report_path=None,
            attempt_count=1 if index == 0 else 2,
        )

    monkeypatch.setattr("repo_doctor.benchmark.benchmark_fixture", fake_fixture)
    path = run_benchmark(
        fixtures,
        ModelSelection(Backend.CODEX, "test-model"),
        lambda prompt: "",
        tmp_path / "results",
        retry_policy=policy,
        write=lambda message: None,
    )
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 2
    assert payload["max_attempts"] == 2
    assert payload["verified_count"] == 2
    assert payload["first_attempt_verified_count"] == 1
    assert payload["verified_fraction_of_planned"] == 2 / 3
    assert [row["attempt_count"] for row in payload["results"]] == [1, 2, 2]


@pytest.mark.parametrize("save_fails", [False, True])
def test_fixture_forwards_policy_and_records_attempts(
    tmp_path, monkeypatch, save_fails
):
    policy = RetryPolicy(max_attempts=3)
    workspace = SimpleNamespace(project_root=tmp_path / "work")
    case = SimpleNamespace(
        production_path="Example.java", test_path="ExampleTest.java", required_tests=()
    )
    result = SimpleNamespace(
        status=RepairAttemptStatus.VERIFIED, attempts=(object(), object())
    )
    repair = Mock(return_value=result)
    monkeypatch.setattr("repo_doctor.benchmark.load_repair_case", lambda path: case)
    monkeypatch.setattr(
        "repo_doctor.benchmark.create_fixture_workspace", lambda path: workspace
    )
    monkeypatch.setattr(
        "repo_doctor.benchmark.ModelProposer", lambda transport: transport
    )
    monkeypatch.setattr("repo_doctor.benchmark.run_fixture_repair", repair)
    save = Mock(
        side_effect=OSError() if save_fails else None,
        return_value=tmp_path / "report.json",
    )
    monkeypatch.setattr("repo_doctor.benchmark.save_repair_report", save)
    row = benchmark_fixture(
        tmp_path,
        ModelSelection(Backend.CODEX, "test-model"),
        lambda prompt: "",
        retry_policy=policy,
    )
    assert repair.call_args.kwargs["retry_policy"] is policy
    assert row.outcome == "verified"
    assert row.attempt_count == 2
    assert bool(row.diagnostic) is save_fails


def test_runner_error_does_not_invent_attempt_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "repo_doctor.benchmark.load_repair_case", Mock(side_effect=RuntimeError())
    )
    row = benchmark_fixture(
        tmp_path, ModelSelection(Backend.CODEX, "test-model"), lambda prompt: ""
    )
    assert row.outcome == "runner_error"
    assert row.attempt_count is None


def test_initial_checkpoint_contains_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "repo_doctor.benchmark.benchmark_fixture", Mock(side_effect=KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        run_benchmark(
            (tmp_path / "fixture",),
            ModelSelection(Backend.CODEX, "test-model"),
            lambda prompt: "",
            tmp_path / "results",
            retry_policy=RetryPolicy(max_attempts=3),
            write=lambda message: None,
        )
    payload = json.loads(
        next((tmp_path / "results").glob("*/summary.json")).read_text()
    )
    assert payload["max_attempts"] == 3
    assert payload["completed_count"] == 0
    assert payload["first_attempt_verified_count"] == 0
    assert payload["complete"] is False
