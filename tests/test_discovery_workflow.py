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


def test_workflow_discovers_paths_and_refreshes_evidence_on_retry(
    repair_setup, monkeypatch
):
    workspace, _ = repair_setup
    (workspace.project_root / TEST_RELATIVE).write_text(
        "class ExampleTest { Example service = new Example(); }", encoding="utf-8"
    )
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=False),
    )
    diagnoses = []

    def propose(diagnosis):
        diagnoses.append(diagnosis)
        if len(diagnoses) == 1:
            return RepairProposal("First hypothesis", "n > 10", "n > 11")
        return RepairProposal("Correct boundary", "n > 11", "n >= 10")

    result = run_fixture_repair(
        workspace,
        None,
        {RELATIVE},
        (REQUIRED,),
        propose_fix=propose,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    assert result.status is RepairAttemptStatus.VERIFIED
    assert runner.run_tests.call_count == 3
    assert len(result.attempts) == 2
    assert diagnoses[0].production_code.relative_path == RELATIVE
    assert diagnoses[0].test_code.relative_path == TEST_RELATIVE
    intermediate = BEFORE.replace(b"n > 10", b"n > 11")
    assert diagnoses[1].production_code.file_sha256 == sha256(intermediate).hexdigest()
    assert (
        result.attempts[1].request.expected_sha256
        == result.attempts[0].patch.after_sha256
    )
    assert (workspace.source_root / RELATIVE).read_bytes() == BEFORE
    assert (workspace.project_root / RELATIVE).read_bytes() == AFTER


def test_ambiguous_discovery_stops_before_model_or_patch(repair_setup, monkeypatch):
    workspace, _ = repair_setup
    (workspace.project_root / TEST_RELATIVE).write_text(
        "class ExampleTest { Example service; Helper helper; }", encoding="utf-8"
    )
    (workspace.project_root / "src/main/java/Helper.java").write_text("class Helper {}")
    runner = install_fake_runner(
        monkeypatch, maven_result(workspace.project_root, failed=True)
    )
    proposer = Mock()
    apply = Mock()
    monkeypatch.setattr(PatchApplier, "apply", apply)
    result = run_fixture_repair(
        workspace,
        None,
        {RELATIVE},
        (REQUIRED,),
        propose_fix=proposer,
    )
    assert result.status is RepairAttemptStatus.DIAGNOSIS_FAILED
    assert "Ambiguous" in result.diagnostics[0]
    assert len(result.attempts) == 1
    proposer.assert_not_called()
    apply.assert_not_called()
    assert runner.run_tests.call_count == 1
