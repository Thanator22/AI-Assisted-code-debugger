from hashlib import sha256
from unittest.mock import Mock

import pytest

from repo_doctor.repair import (
    RepairAttemptStatus,
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


RELATIVE = "src/main/java/Example.java"
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
        Issue(
            test_name="boundary",
            class_name="ExampleTest",
            kind=IssueKind.FAILURE,
            message="Expected true",
            exception_type="AssertionError",
            stack_trace="",
        ),
    ) if failed else ()

    return MavenTestResult(
        execution=CommandResult(
            status=(
                CommandStatus.NON_ZERO_EXIT
                if failed
                else CommandStatus.SUCCESS
            ),
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
                test_cases=(
                    CaseResult("ExampleTest", "boundary", case_status),
                ),
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

def test_rejected_baseline_never_calls_patcher(
    repair_setup, monkeypatch
):
    workspace, request = repair_setup

    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=False),
    )

    apply = Mock(
        side_effect=AssertionError("Patcher must not be called")
    )
    monkeypatch.setattr(PatchApplier, "apply", apply)

    result = run_fixture_repair(
        workspace,
        request,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
    )

    assert result.status is RepairAttemptStatus.BASELINE_REJECTED
    assert result.patch is None
    assert result.verification is None
    apply.assert_not_called()
    assert runner.run_tests.call_count == 1


def test_stale_patch_prevents_verification(
    repair_setup, monkeypatch
):
    workspace, request = repair_setup

    stale_request = ReplacementRequest(
        relative_path=request.relative_path,
        expected_sha256=sha256(b"an older version").hexdigest(),
        old_text=request.old_text,
        new_text=request.new_text,
    )

    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
    )

    result = run_fixture_repair(
        workspace,
        stale_request,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
    )

    assert result.status is RepairAttemptStatus.PATCH_REJECTED
    assert result.patch.status is PatchStatus.STALE_CONTENT
    assert result.verification is None
    assert runner.run_tests.call_count == 1
    assert (workspace.project_root / RELATIVE).read_bytes() == BEFORE


def test_valid_attempt_applies_patch_and_verifies(
    repair_setup, monkeypatch
):
    workspace, request = repair_setup

    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=False),
    )

    result = run_fixture_repair(
        workspace,
        request,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
    )

    assert result.status is RepairAttemptStatus.VERIFIED
    assert result.patch.status is PatchStatus.APPLIED
    assert result.verification is not None
    assert result.diagnostics == ()
    assert runner.run_tests.call_count == 2
    assert (workspace.project_root / RELATIVE).read_bytes() == AFTER
    assert (workspace.source_root / RELATIVE).read_bytes() == BEFORE