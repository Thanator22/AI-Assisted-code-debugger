import json
from pathlib import Path

from repo_doctor.backend_selection import Backend, ModelSelection
from repo_doctor.benchmark import BenchmarkRow, run_benchmark


def test_records_failure_and_continues(tmp_path, monkeypatch):
    fixtures = (
        tmp_path / "first",
        tmp_path / "second",
    )
    calls = []

    def fake_attempt(fixture, selection, transport):
        calls.append(fixture)

        return BenchmarkRow(
            fixture=str(fixture),
            outcome=(
                "runner_error"
                if fixture == fixtures[0]
                else "verified"
            ),
            duration_seconds=0.5,
            workspace=None,
            report_path=None,
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

    def fake_attempt(fixture, selection, transport):
        if fixture == fixtures[1]:
            raise KeyboardInterrupt()

        return BenchmarkRow(
            fixture=str(fixture),
            outcome="verified",
            duration_seconds=0.5,
            workspace=None,
            report_path=None,
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