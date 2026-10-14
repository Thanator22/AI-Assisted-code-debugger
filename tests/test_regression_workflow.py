from dataclasses import replace
from unittest.mock import Mock

import pytest

from repo_doctor.diagnosis import RepairProposal
from repo_doctor.regression import (
    ASSERTION_MARKER,
    CLASS_NAME,
    METHOD_NAME,
    RegressionSpec,
    ModelRegressionGenerator,
)
from repo_doctor.repair import (
    RequiredTest,
    RetryPolicy,
    RepairAttemptStatus,
    run_fixture_repair,
)
from repo_doctor.tools.build import MavenTestResult
from repo_doctor.tools.process import CommandResult, CommandStatus
from repo_doctor.tools.test_reports import (
    ReportCollectionResult,
    ReportCollectionStatus,
    TestReport as Report,
    TestCaseResult as Case,
    TestCaseStatus as Status,
    TestIssue as Issue,
    TestIssueKind as Kind,
)
from repo_doctor.tools.workspace import RepairWorkspace
from repo_doctor.backend_selection import Backend, ModelSelection
from repo_doctor.repair_reporting import build_repair_report
from repo_doctor.tools.patching import PatchApplier, ReplacementRequest, PatchStatus
from repo_doctor.tools.repository import Repository
from hashlib import sha256

PRODUCTION = "src/main/java/Example.java"
TEST = "src/test/java/ExampleTest.java"
BEFORE = "class Example { int total(int n) { return n > 10 ? 90 : 100; } }"
SPEC = RegressionSpec("Threshold is inclusive", "total", False, (10,), 90)


@pytest.fixture
def workspace(tmp_path):
    roots = (tmp_path / "source", tmp_path / "repair")
    for root in roots:
        (root / "src/main/java").mkdir(parents=True)
        (root / "src/test/java").mkdir(parents=True)
        (root / PRODUCTION).write_text(BEFORE)
        (root / TEST).write_text(
            "class ExampleTest { Example service = new Example(); }"
        )
        (root / "pom.xml").write_text("<project/>")
        (root / "bug-report.md").write_text("Return 90 at quantities of 10 or above.")
    return RepairWorkspace(*roots)


def run_result(root, *, old=Status.FAILED, generated=None):
    cases = [Case("ExampleTest", "boundary", old)]
    if generated is not None:
        cases.append(Case(CLASS_NAME, METHOD_NAME, generated))
    issues = tuple(
        Issue(
            c.test_name,
            c.class_name,
            Kind.FAILURE if c.status is Status.FAILED else Kind.ERROR,
            ASSERTION_MARKER + " ==> expected: <90> but was: <100>",
            (
                "org.opentest4j.AssertionFailedError"
                if c.status is Status.FAILED
                else "IllegalStateException"
            ),
            "",
        )
        for c in cases
        if c.status in (Status.FAILED, Status.ERROR)
    )
    failures = sum(c.status is Status.FAILED for c in cases)
    errors = sum(c.status is Status.ERROR for c in cases)
    bad = bool(failures + errors)
    return MavenTestResult(
        CommandResult(
            status=CommandStatus.NON_ZERO_EXIT if bad else CommandStatus.SUCCESS,
            command=("mvn", "test"),
            working_directory=str(root),
            exit_code=1 if bad else 0,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            output_truncated=False,
        ),
        ReportCollectionResult(
            status=ReportCollectionStatus.AVAILABLE,
            report=Report(
                total_tests=len(cases),
                failures=failures,
                errors=errors,
                skipped=sum(c.status is Status.SKIPPED for c in cases),
                issues=issues,
                test_cases=tuple(cases),
            ),
        ),
        root / "target/mock",
    )


def run(workspace, monkeypatch, results, *, proposer=None, generator=None, attempts=1):
    runner = Mock()
    runner.run_tests.side_effect = results
    monkeypatch.setattr("repo_doctor.repair.MavenTestRunner", lambda root: runner)
    generator = generator if generator is not None else Mock(return_value=SPEC)
    proposer = (
        proposer
        if proposer is not None
        else Mock(return_value=RepairProposal("Include equality", "n > 10", "n >= 10"))
    )
    result = run_fixture_repair(
        workspace,
        None,
        {PRODUCTION},
        (RequiredTest("ExampleTest", "boundary"),),
        propose_fix=proposer,
        generate_regression=generator,
        retry_policy=RetryPolicy(attempts),
    )
    return result, runner, generator, proposer


def test_verified_requires_red_then_green_and_preserves_original(
    workspace, monkeypatch
):
    root = workspace.project_root
    result, runner, generator, proposer = run(
        workspace,
        monkeypatch,
        [
            run_result(root),
            run_result(root, generated=Status.FAILED),
            run_result(root, old=Status.PASSED, generated=Status.PASSED),
        ],
    )
    assert result.status is RepairAttemptStatus.VERIFIED
    assert result.regression_accepted
    assert runner.run_tests.call_count == 3
    generator.assert_called_once()
    proposer.assert_called_once()
    assert (workspace.source_root / PRODUCTION).read_text() == BEFORE
    assert not (workspace.source_root / result.regression.relative_path).exists()
    payload = build_repair_report(result, ModelSelection(Backend.CODEX, "test-model"))
    assert payload["regression"]["reproduction_accepted"]
    assert payload["regression"]["reproduction"]["tests"]["failures"] == 2
    assert payload["verification"]["tests"]["passed"] == 2
    assert "--- /dev/null" in payload["regression"]["diff"]["content"]
    # The normal patcher still cannot modify even the generated test.
    generated = result.regression
    patch = PatchApplier(workspace, {PRODUCTION}).apply(
        ReplacementRequest(
            generated.relative_path, generated.file_sha256, "90.0", "100.0"
        )
    )
    assert patch.status is PatchStatus.NOT_ALLOWED


@pytest.mark.parametrize(
    "problem",
    [
        "passing",
        "error",
        "skipped",
        "missing_case",
        "timeout",
        "missing_reports",
        "wrong_assertion",
        "changed_old",
        "counts",
    ],
)
def test_unusable_red_run_stops_before_production_patch(
    workspace, monkeypatch, problem
):
    root = workspace.project_root
    red = run_result(root, generated=Status.FAILED)
    if problem in ("passing", "error", "skipped", "missing_case"):
        red = run_result(
            root,
            generated={
                "passing": Status.PASSED,
                "error": Status.ERROR,
                "skipped": Status.SKIPPED,
                "missing_case": None,
            }[problem],
        )
    elif problem == "timeout":
        red = replace(
            red,
            execution=replace(
                red.execution, status=CommandStatus.TIMED_OUT, exit_code=None
            ),
        )
    elif problem == "missing_reports":
        red = replace(
            red,
            reports=ReportCollectionResult(
                status=ReportCollectionStatus.MISSING, report=None
            ),
        )
    elif problem == "changed_old":
        red = run_result(root, old=Status.PASSED, generated=Status.FAILED)
    elif problem == "counts":
        red = replace(
            red,
            reports=replace(
                red.reports, report=replace(red.reports.report, total_tests=99)
            ),
        )
    else:
        report = red.reports.report
        red = replace(
            red,
            reports=replace(
                red.reports,
                report=replace(
                    report,
                    issues=(
                        report.issues[0],
                        replace(report.issues[1], message="different assertion"),
                    ),
                ),
            ),
        )
    result, runner, generator, proposer = run(
        workspace, monkeypatch, [run_result(root), red]
    )
    assert result.status is RepairAttemptStatus.REGRESSION_REJECTED
    assert not result.regression_accepted
    assert result.patch is None
    proposer.assert_not_called()
    assert (root / PRODUCTION).read_text() == BEFORE


def test_retry_freezes_test_even_when_only_generated_case_still_fails(
    workspace, monkeypatch
):
    root = workspace.project_root
    proposer = Mock(
        side_effect=[
            RepairProposal("First", "n > 10", "n > 11"),
            RepairProposal("Fix", "n > 11", "n >= 10"),
        ]
    )
    result, runner, generator, _ = run(
        workspace,
        monkeypatch,
        [
            run_result(root),
            run_result(root, generated=Status.FAILED),
            run_result(root, old=Status.PASSED, generated=Status.FAILED),
            run_result(root, old=Status.PASSED, generated=Status.PASSED),
        ],
        proposer=proposer,
        attempts=2,
    )
    assert result.status is RepairAttemptStatus.VERIFIED
    assert len(result.attempts) == 2
    generator.assert_called_once()
    assert proposer.call_args_list[1].args[0].failure.test_name == METHOD_NAME
    assert (
        root / result.regression.relative_path
    ).read_text() == result.regression.content
    assert runner.run_tests.call_count == 4


def test_mutating_frozen_test_rejects_even_a_passing_run(workspace, monkeypatch):
    root = workspace.project_root

    def green():
        (root / "src/test/java" / (CLASS_NAME + ".java")).write_text("// tampered")
        return run_result(root, old=Status.PASSED, generated=Status.PASSED)

    values = iter(
        [
            lambda: run_result(root),
            lambda: run_result(root, generated=Status.FAILED),
            green,
        ]
    )
    result, *_ = run(workspace, monkeypatch, lambda: next(values)())
    assert result.status is RepairAttemptStatus.INTEGRITY_FAILED


@pytest.mark.parametrize("generated", [None, Status.SKIPPED])
def test_green_suite_cannot_omit_or_skip_generated_test(
    workspace, monkeypatch, generated
):
    root = workspace.project_root
    result, *_ = run(
        workspace,
        monkeypatch,
        [
            run_result(root),
            run_result(root, generated=Status.FAILED),
            run_result(root, old=Status.PASSED, generated=generated),
        ],
    )
    assert result.status is RepairAttemptStatus.VERIFICATION_FAILED


def test_existing_generated_filename_is_never_overwritten(workspace, monkeypatch):
    root = workspace.project_root
    path = root / "src/test/java" / (CLASS_NAME + ".java")
    path.write_text("existing content")
    result, runner, _, proposer = run(workspace, monkeypatch, [run_result(root)])
    assert result.status is RepairAttemptStatus.REGRESSION_REJECTED
    assert path.read_text() == "existing content"
    proposer.assert_not_called()
    assert runner.run_tests.call_count == 1


def test_generator_uses_common_transport_with_data_only_contract(workspace):
    from repo_doctor.diagnosis import build_diagnosis_input
    from repo_doctor.regression import ModelRegressionGenerator
    import json

    issue = run_result(workspace.project_root).reports.report.issues[0]
    diagnosis = build_diagnosis_input(
        Repository(workspace.project_root), issue, PRODUCTION, TEST
    )
    transport = Mock(
        return_value=json.dumps(
            dict(
                explanation="Boundary",
                method="total",
                static=False,
                arguments=[10],
                expected=90,
            )
        )
    )
    assert ModelRegressionGenerator(transport)(diagnosis) == RegressionSpec(
        "Boundary", "total", False, (10,), 90
    )
    prompt = transport.call_args.args[0]
    assert "Do not supply Java code" in prompt.instructions
    assert "file_sha256" not in prompt.evidence
