from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repo_doctor import cli
from repo_doctor.backend_selection import Backend, ModelSelection
from repo_doctor.repair import RepairAttemptStatus, RetryPolicy


def test_default_attempt_budget_is_one():
    assert cli.parse_args([]).max_attempts == 1


def test_explicit_attempt_budget():
    assert cli.parse_args(["--max-attempts", "3"]).max_attempts == 3


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid"])
def test_invalid_budget_exits_before_authentication(monkeypatch, capsys, value):
    constructors = []
    for name in ("ClaudeAdapter", "CopilotAdapter", "CodexAdapter"):
        constructor = Mock()
        constructors.append(constructor)
        monkeypatch.setattr(cli, name, constructor)
    load = Mock()
    monkeypatch.setattr(cli, "load_selection", load)
    with pytest.raises(SystemExit) as error:
        cli.main(["--max-attempts", value])
    assert error.value.code == 2
    assert "positive integer" in capsys.readouterr().err
    for constructor in constructors:
        constructor.assert_not_called()
    load.assert_not_called()


def test_menu_forwards_same_policy_to_both_paths(tmp_path, monkeypatch):
    fixture = tmp_path / "fixtures" / "example"
    fixture.mkdir(parents=True)
    (fixture / "repair-case.json").write_text("{}")
    selector = SimpleNamespace(
        selection=ModelSelection(Backend.CODEX, "test-model"),
        transport=lambda prompt: "",
    )
    adapters = []
    for name in ("ClaudeAdapter", "CopilotAdapter", "CodexAdapter"):
        adapter = Mock()
        adapters.append(adapter)
        monkeypatch.setattr(cli, name, Mock(return_value=adapter))
    monkeypatch.setattr(cli, "BackendSelector", lambda adapters: selector)
    monkeypatch.setattr(cli, "load_selection", lambda: None)
    monkeypatch.setattr(cli, "choose_number", Mock(side_effect=[2, 3, 4]))
    selected = Mock()
    benchmark = Mock()
    monkeypatch.setattr(cli, "run_selected_repair", selected)
    monkeypatch.setattr(cli, "run_benchmark", benchmark)
    cli.main(
        [
            "--max-attempts",
            "3",
            "--fixture",
            str(fixture),
            "--benchmark-root",
            str(fixture.parent),
            "--results-root",
            str(tmp_path / "results"),
        ]
    )
    selected.assert_called_once()
    benchmark.assert_called_once()
    policy = selected.call_args.kwargs["retry_policy"]
    assert policy.max_attempts == 3
    assert benchmark.call_args.kwargs["retry_policy"] is policy
    assert benchmark.call_args.kwargs["fixtures"] == (fixture,)
    for adapter in adapters:
        adapter.close.assert_called_once()


def test_selected_repair_passes_policy_to_workflow(tmp_path, monkeypatch, capsys):
    policy = RetryPolicy(max_attempts=2)
    selector = SimpleNamespace(
        selection=ModelSelection(Backend.CODEX, "test-model"),
        transport=lambda prompt: "",
    )
    case = SimpleNamespace(
        production_path="Example.java", test_path="ExampleTest.java", required_tests=()
    )
    workspace = SimpleNamespace(project_root=tmp_path / "work")
    result = SimpleNamespace(
        status=RepairAttemptStatus.PROPOSAL_FAILED,
        baseline=object(),
        patch=None,
        verification=None,
        attempts=(object(), object()),
        diagnostics=(),
    )
    repair = Mock(return_value=result)
    monkeypatch.setattr(cli, "load_repair_case", lambda fixture: case)
    monkeypatch.setattr(cli, "create_fixture_workspace", lambda fixture: workspace)
    monkeypatch.setattr(cli, "ModelProposer", lambda transport: transport)
    monkeypatch.setattr(cli, "run_fixture_repair", repair)
    monkeypatch.setattr(
        cli, "classify_test_run", lambda run: SimpleNamespace(value="tests_failed")
    )
    monkeypatch.setattr(
        cli, "save_repair_report", lambda result, selection: tmp_path / "report.json"
    )
    cli.run_selected_repair(selector, tmp_path, retry_policy=policy)
    repair.assert_called_once()
    assert repair.call_args.kwargs["retry_policy"] is policy
    assert "Attempts: 2/2" in capsys.readouterr().out
