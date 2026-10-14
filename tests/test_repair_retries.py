from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

import pytest
import json

from repo_doctor.backend_selection import Backend, ModelSelection
from repo_doctor.repair_reporting import (
    build_repair_report,
    save_repair_report,
)

from repo_doctor.repair import (
    RepairAttemptStatus,
    RetryPolicy,
    RequiredTest,
    run_fixture_repair,
)
from repo_doctor.tools.build import MavenTestResult
from repo_doctor.tools.patching import (
    PatchApplier,
    PatchStatus,
    ReplacementRequest,
)
from repo_doctor.tools.process import CommandResult, CommandStatus
from repo_doctor.tools.test_reports import (
    ReportCollectionResult,
    ReportCollectionStatus,
    TestCaseResult as CaseResult,
    TestCaseStatus as CaseStatus,
    TestIssue as Issue,
    TestIssueKind as IssueKind,
    TestReport as Report,
)
from repo_doctor.tools.workspace import create_fixture_workspace
from repo_doctor.diagnosis import RepairProposal

RELATIVE = "src/main/java/Example.java"
TEST_RELATIVE = "src/test/java/ExampleTest.java"
REQUIRED = RequiredTest("ExampleTest", "boundary")
BEFORE = b"class Example { boolean check(int n) { return n > 10; } }"
AFTER = b"class Example { boolean check(int n) { return n >= 10; } }"


@pytest.fixture
def repair_setup(tmp_path):
    source = tmp_path / "fixture"
    java_file = source / RELATIVE
    java_file.parent.mkdir(parents=True)
    java_file.write_bytes(BEFORE)

    (source / "pom.xml").write_text("<project/>", encoding="utf-8")
    (source / "bug-report.md").write_text(
        "Include the threshold",
        encoding="utf-8",
    )

    test_file = source / TEST_RELATIVE
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "class ExampleTest { void boundary() {} }\n",
        encoding="utf-8",
    )

    workspace = create_fixture_workspace(source)

    request = ReplacementRequest(
        relative_path=RELATIVE,
        expected_sha256=sha256(BEFORE).hexdigest(),
        old_text="n > 10",
        new_text="n >= 10",
    )

    return workspace, request


def maven_result(project_root, *, failed):
    case_status = CaseStatus.FAILED if failed else CaseStatus.PASSED

    issues = (
        (
            Issue(
                test_name="boundary",
                class_name="ExampleTest",
                kind=IssueKind.FAILURE,
                message="Expected true",
                exception_type="AssertionError",
                stack_trace="",
            ),
        )
        if failed
        else ()
    )

    return MavenTestResult(
        execution=CommandResult(
            status=(CommandStatus.NON_ZERO_EXIT if failed else CommandStatus.SUCCESS),
            command=("mvn", "test"),
            working_directory=str(project_root),
            exit_code=1 if failed else 0,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            output_truncated=False,
        ),
        reports=ReportCollectionResult(
            status=ReportCollectionStatus.AVAILABLE,
            report=Report(
                total_tests=1,
                failures=int(failed),
                errors=0,
                skipped=0,
                issues=issues,
                test_cases=(CaseResult("ExampleTest", "boundary", case_status),),
            ),
        ),
        report_directory=project_root / "target" / "mock-reports",
    )


def install_fake_runner(monkeypatch, *results):
    runner = Mock()
    runner.run_tests.side_effect = results

    monkeypatch.setattr(
        "repo_doctor.repair.MavenTestRunner",
        lambda project_root: runner,
    )

    return runner


INTERMEDIATE = BEFORE.replace(b"n > 10", b"n > 11")


def first_proposal(diagnosis):
    return RepairProposal("First hypothesis", "n > 10", "n > 11")


def corrected_proposal(diagnosis):
    return RepairProposal("Correct the boundary", "n > 11", "n >= 10")


def run_with_proposer(workspace, proposer, budget=2):
    return run_fixture_repair(
        workspace,
        request=None,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
        propose_fix=proposer,
        production_path=RELATIVE,
        test_path=TEST_RELATIVE,
        retry_policy=RetryPolicy(max_attempts=budget),
    )


def test_retry_uses_fresh_source_and_latest_failure(repair_setup, monkeypatch):
    workspace, _ = repair_setup
    baseline = maven_result(workspace.project_root, failed=True)
    failed = maven_result(workspace.project_root, failed=True)
    failed = replace(
        failed,
        reports=replace(
            failed.reports,
            report=replace(
                failed.reports.report,
                issues=(
                    replace(
                        failed.reports.report.issues[0],
                        message="Still wrong after edit",
                    ),
                ),
            ),
        ),
    )
    runner = install_fake_runner(
        monkeypatch,
        baseline,
        failed,
        maven_result(workspace.project_root, failed=False),
    )
    diagnoses = []

    def propose(diagnosis):
        diagnoses.append(diagnosis)
        return (
            first_proposal(diagnosis)
            if len(diagnoses) == 1
            else corrected_proposal(diagnosis)
        )

    result = run_with_proposer(workspace, propose)
    assert result.status is RepairAttemptStatus.VERIFIED
    assert runner.run_tests.call_count == 3
    assert len(result.attempts) == 2
    assert [a.number for a in result.attempts] == [1, 2]
    assert [a.status for a in result.attempts] == [
        RepairAttemptStatus.VERIFICATION_FAILED,
        RepairAttemptStatus.VERIFIED,
    ]
    assert diagnoses[1].failure.message == "Still wrong after edit"
    assert diagnoses[1].production_code.content == INTERMEDIATE.decode()
    assert diagnoses[1].production_code.file_sha256 == sha256(INTERMEDIATE).hexdigest()
    assert (
        result.attempts[1].request.expected_sha256
        == result.attempts[0].patch.after_sha256
    )
    assert (workspace.source_root / RELATIVE).read_bytes() == BEFORE
    assert (workspace.project_root / RELATIVE).read_bytes() == AFTER

    payload = build_repair_report(result, ModelSelection(Backend.CODEX, "test-model"))
    assert payload["schema_version"] == 2
    assert payload["attempt_count"] == 2
    assert payload["attempts"][0]["verification"]["tests"]["failures"] == 1
    assert payload["attempts"][1]["verification"]["tests"]["passed"] == 1
    assert payload["diff"]["status"] == "available"
    assert "n > 10" in payload["diff"]["content"]
    assert "n >= 10" in payload["diff"]["content"]
    assert "n > 11" not in payload["diff"]["content"]
    path = save_repair_report(result, ModelSelection(Backend.CODEX, "test-model"))
    assert json.loads(path.read_text())["attempt_count"] == 2
    (workspace.project_root / RELATIVE).write_bytes(AFTER + b"// later edit")
    assert (
        build_repair_report(result, ModelSelection(Backend.CODEX, "test-model"))[
            "diff"
        ]["status"]
        == "unavailable"
    )


@pytest.mark.parametrize("budget", [1, 2, 5])
def test_attempt_budget_is_enforced(repair_setup, monkeypatch, budget):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        *[maven_result(workspace.project_root, failed=True) for _ in range(budget + 1)],
    )
    count = 0

    def propose(diagnosis):
        nonlocal count
        count += 1
        return RepairProposal("Try a boundary", f"n > {9 + count}", f"n > {10 + count}")

    result = run_with_proposer(workspace, propose, budget)
    assert result.status is RepairAttemptStatus.VERIFICATION_FAILED
    assert count == budget
    assert runner.run_tests.call_count == budget + 1
    assert len(result.attempts) == budget


@pytest.mark.parametrize(
    "problem",
    ["timeout", "missing", "build_failure", "skipped", "identities", "counts"],
)
def test_unusable_verification_never_retries(repair_setup, monkeypatch, problem):
    workspace, _ = repair_setup
    verification = maven_result(workspace.project_root, failed=True)
    if problem == "timeout":
        verification = replace(
            verification,
            execution=replace(
                verification.execution,
                status=CommandStatus.TIMED_OUT,
                exit_code=None,
            ),
        )
    elif problem == "missing":
        verification = replace(
            verification,
            reports=ReportCollectionResult(
                status=ReportCollectionStatus.MISSING,
                report=None,
            ),
        )
    elif problem == "build_failure":
        verification = maven_result(workspace.project_root, failed=False)
        verification = replace(
            verification,
            execution=replace(
                verification.execution,
                status=CommandStatus.NON_ZERO_EXIT,
                exit_code=1,
            ),
        )
    else:
        report = verification.reports.report
        if problem == "skipped":
            report = replace(
                report,
                total_tests=2,
                skipped=1,
                test_cases=(
                    *report.test_cases,
                    CaseResult("ExampleTest", "other", CaseStatus.SKIPPED),
                ),
            )
        elif problem == "identities":
            report = replace(
                report,
                test_cases=(CaseResult("OtherTest", "boundary", CaseStatus.FAILED),),
            )
        else:
            report = replace(report, total_tests=2)
        verification = replace(
            verification, reports=replace(verification.reports, report=report)
        )
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        verification,
    )
    proposer = Mock(side_effect=first_proposal)
    result = run_with_proposer(workspace, proposer)
    assert result.status is RepairAttemptStatus.VERIFICATION_FAILED
    proposer.assert_called_once()
    assert runner.run_tests.call_count == 2
    assert len(result.attempts) == 1


def test_later_proposal_failure_retains_prior_patch(repair_setup, monkeypatch):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=True),
    )
    proposer = Mock(side_effect=[first_proposal(None), RuntimeError("backend failed")])
    result = run_with_proposer(workspace, proposer)
    assert result.status is RepairAttemptStatus.PROPOSAL_FAILED
    assert len(result.attempts) == 2
    assert result.attempts[0].patch.status is PatchStatus.APPLIED
    assert result.attempts[1].patch is None
    assert result.patch is None
    assert result.verification is None
    assert runner.run_tests.call_count == 2
    payload = build_repair_report(result, ModelSelection(Backend.CODEX, "test-model"))
    assert payload["diff"]["status"] == "available"
    assert "n > 11" in payload["diff"]["content"]


def test_integrity_failure_on_retry_stops_before_second_patch(
    repair_setup, monkeypatch
):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=True),
    )
    calls = 0

    def propose(diagnosis):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_proposal(diagnosis)
        (workspace.project_root / TEST_RELATIVE).write_text("// unauthorized change")
        return corrected_proposal(diagnosis)

    result = run_with_proposer(workspace, propose)
    assert result.status is RepairAttemptStatus.INTEGRITY_FAILED
    assert len(result.attempts) == 2
    assert result.attempts[1].patch is None
    assert (workspace.project_root / RELATIVE).read_bytes() == INTERMEDIATE
    assert runner.run_tests.call_count == 2


def test_supplied_request_is_never_replayed(repair_setup, monkeypatch):
    workspace, request = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=True),
    )
    result = run_fixture_repair(
        workspace,
        request,
        {RELATIVE},
        (REQUIRED,),
        retry_policy=RetryPolicy(max_attempts=3),
    )
    assert result.status is RepairAttemptStatus.VERIFICATION_FAILED
    assert len(result.attempts) == 1
    assert runner.run_tests.call_count == 2


def test_rejected_baseline_has_no_attempts(repair_setup, monkeypatch):
    workspace, _ = repair_setup
    install_fake_runner(monkeypatch, maven_result(workspace.project_root, failed=False))
    proposer = Mock()
    result = run_with_proposer(workspace, proposer)
    assert result.status is RepairAttemptStatus.BASELINE_REJECTED
    assert result.attempts == ()
    proposer.assert_not_called()


def test_retry_can_focus_on_a_newly_failing_test(repair_setup, monkeypatch):
    workspace, _ = repair_setup
    baseline = maven_result(workspace.project_root, failed=True)
    baseline = replace(
        baseline,
        reports=replace(
            baseline.reports,
            report=replace(
                baseline.reports.report,
                total_tests=2,
                test_cases=(
                    *baseline.reports.report.test_cases,
                    CaseResult("ExampleTest", "regression", CaseStatus.PASSED),
                ),
            ),
        ),
    )
    failed = maven_result(workspace.project_root, failed=True)
    failed = replace(
        failed,
        reports=replace(
            failed.reports,
            report=replace(
                failed.reports.report,
                total_tests=2,
                test_cases=(
                    CaseResult("ExampleTest", "boundary", CaseStatus.PASSED),
                    CaseResult("ExampleTest", "regression", CaseStatus.FAILED),
                ),
                issues=(
                    replace(failed.reports.report.issues[0], test_name="regression"),
                ),
            ),
        ),
    )
    passed = maven_result(workspace.project_root, failed=False)
    passed = replace(
        passed,
        reports=replace(
            passed.reports,
            report=replace(
                passed.reports.report,
                total_tests=2,
                test_cases=(
                    *passed.reports.report.test_cases,
                    CaseResult("ExampleTest", "regression", CaseStatus.PASSED),
                ),
            ),
        ),
    )
    install_fake_runner(monkeypatch, baseline, failed, passed)
    focused = []

    def propose(diagnosis):
        focused.append(diagnosis.failure.test_name)
        return (
            first_proposal(diagnosis)
            if len(focused) == 1
            else corrected_proposal(diagnosis)
        )

    result = run_with_proposer(workspace, propose)
    assert result.status is RepairAttemptStatus.VERIFIED
    assert focused == ["boundary", "regression"]


def test_broken_patch_history_cannot_produce_a_diff(repair_setup, monkeypatch):
    workspace, _ = repair_setup
    install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=False),
    )
    result = run_with_proposer(
        workspace,
        Mock(
            side_effect=[
                first_proposal(None),
                corrected_proposal(None),
            ]
        ),
    )
    second = result.attempts[1]
    wrong_hash = sha256(b"unrelated source").hexdigest()
    broken = replace(
        second,
        request=replace(second.request, expected_sha256=wrong_hash),
        patch=replace(second.patch, before_sha256=wrong_hash),
    )
    result = replace(result, attempts=(result.attempts[0], broken))
    payload = build_repair_report(result, ModelSelection(Backend.CODEX, "test-model"))
    assert payload["diff"]["status"] == "unavailable"
    assert payload["diff"]["content"] is None
