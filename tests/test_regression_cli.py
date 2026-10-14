from types import SimpleNamespace
from unittest.mock import Mock

from repo_doctor import cli, benchmark
from repo_doctor.backend_selection import Backend, ModelSelection
from repo_doctor.regression import ModelRegressionGenerator
from repo_doctor.repair import RepairAttemptStatus


def test_regression_flag_is_explicit():
    assert not cli.parse_args([]).generate_regression
    assert cli.parse_args(["--generate-regression"]).generate_regression


def test_menu_forwards_regression_to_both_actions(tmp_path, monkeypatch):
    fixture = tmp_path / "fixtures" / "example"
    fixture.mkdir(parents=True)
    (fixture / "repair-case.json").write_text("{}")
    selector = SimpleNamespace(
        selection=ModelSelection(Backend.CODEX, "test-model"), transport=Mock()
    )
    for name in ("ClaudeAdapter", "CodexAdapter", "CopilotAdapter"):
        monkeypatch.setattr(cli, name, Mock())
    monkeypatch.setattr(cli, "BackendSelector", lambda adapters: selector)
    monkeypatch.setattr(cli, "load_selection", lambda: None)
    monkeypatch.setattr(cli, "choose_number", Mock(side_effect=[2, 3, 4]))
    selected, batch = Mock(), Mock()
    monkeypatch.setattr(cli, "run_selected_repair", selected)
    monkeypatch.setattr(cli, "run_benchmark", batch)
    cli.main(["--generate-regression", "--benchmark-root", str(fixture.parent)])
    assert selected.call_args.kwargs["generate_regression"] is True
    assert batch.call_args.kwargs["generate_regression"] is True


def test_benchmark_and_single_repair_use_selected_transport(tmp_path, monkeypatch):
    selection = ModelSelection(Backend.CODEX, "test-model")
    transport = Mock()
    case = SimpleNamespace(
        production_path="src/main/java/Example.java",
        test_path="src/test/java/ExampleTest.java",
        required_tests=(),
    )
    workspace = SimpleNamespace(project_root=tmp_path)
    result = SimpleNamespace(
        status=RepairAttemptStatus.PROPOSAL_FAILED,
        baseline=object(),
        patch=None,
        verification=None,
        attempts=(),
        diagnostics=(),
    )
    for module in (cli, benchmark):
        monkeypatch.setattr(module, "load_repair_case", lambda fixture: case)
        monkeypatch.setattr(
            module, "create_fixture_workspace", lambda fixture: workspace
        )
        monkeypatch.setattr(
            module, "save_repair_report", lambda *args: tmp_path / "report.json"
        )
        repair = Mock(return_value=result)
        monkeypatch.setattr(module, "run_fixture_repair", repair)
        if module is cli:
            monkeypatch.setattr(
                cli,
                "classify_test_run",
                lambda run: SimpleNamespace(value="tests_failed"),
            )
            cli.run_selected_repair(
                SimpleNamespace(selection=selection, transport=transport),
                tmp_path,
                generate_regression=True,
            )
        else:
            benchmark.benchmark_fixture(
                tmp_path, selection, transport, generate_regression=True
            )
        callback = repair.call_args.kwargs["generate_regression"]
        assert isinstance(callback, ModelRegressionGenerator)
        assert callback.transport is transport


def test_benchmark_persists_and_forwards_mode(tmp_path, monkeypatch):
    import json

    fixture = tmp_path / "example"
    run = Mock(
        return_value=benchmark.BenchmarkRow(
            str(fixture), "verified", 1.0, "work", "report", attempt_count=1
        )
    )
    monkeypatch.setattr(benchmark, "benchmark_fixture", run)
    path = benchmark.run_benchmark(
        (fixture,),
        ModelSelection(Backend.CODEX, "test-model"),
        Mock(),
        tmp_path / "results",
        generate_regression=True,
    )
    assert run.call_args.kwargs["generate_regression"] is True
    assert json.loads(path.read_text())["regression_required"] is True
