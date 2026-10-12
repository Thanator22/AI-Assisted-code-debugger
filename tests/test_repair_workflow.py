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
    
def known_proposal(diagnosis):
    return RepairProposal(
        explanation="The bulk threshold includes equality.",
        old_text="n > 10",
        new_text="n >= 10",
    )


def test_proposer_runs_after_baseline_and_repair_is_verified(
    repair_setup, monkeypatch
):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
        maven_result(workspace.project_root, failed=False),
    )

    def propose(diagnosis):
        # Exactly one baseline run has occurred; no edit has occurred.
        assert runner.run_tests.call_count == 1
        assert (workspace.project_root / RELATIVE).read_bytes() == BEFORE

        assert diagnosis.failure.test_name == REQUIRED.test_name
        assert diagnosis.production_code.content == BEFORE.decode("utf-8")
        assert diagnosis.production_code.file_sha256 == sha256(
            BEFORE
        ).hexdigest()
        assert diagnosis.test_code.relative_path == TEST_RELATIVE

        return known_proposal(diagnosis)

    proposer = Mock(side_effect=propose)

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
        propose_fix=proposer,
        production_path=RELATIVE,
        test_path=TEST_RELATIVE,
    )

    assert result.status is RepairAttemptStatus.VERIFIED
    proposer.assert_called_once()
    assert runner.run_tests.call_count == 2
    assert (workspace.project_root / RELATIVE).read_bytes() == AFTER
    assert (workspace.source_root / RELATIVE).read_bytes() == BEFORE


def test_rejected_baseline_never_calls_proposer(
    repair_setup, monkeypatch
):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=False),
    )
    proposer = Mock()

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
        propose_fix=proposer,
        production_path=RELATIVE,
        test_path=TEST_RELATIVE,
    )

    assert result.status is RepairAttemptStatus.BASELINE_REJECTED
    proposer.assert_not_called()
    assert result.patch is None
    assert runner.run_tests.call_count == 1


@pytest.mark.parametrize(
    "raises_after_change",
    [False, True],
)
def test_proposer_input_changes_stop_repair(
    repair_setup, monkeypatch, raises_after_change
):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
    )
    apply = Mock()
    monkeypatch.setattr(PatchApplier, "apply", apply)

    def propose(diagnosis):
        (workspace.project_root / RELATIVE).write_bytes(
            BEFORE + b"\n// unexpected change"
        )
        if raises_after_change:
            raise RuntimeError("Proposer failed")
        return known_proposal(diagnosis)

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
        propose_fix=propose,
        production_path=RELATIVE,
        test_path=TEST_RELATIVE,
    )

    assert result.status is RepairAttemptStatus.INTEGRITY_FAILED
    apply.assert_not_called()
    assert result.patch is None
    assert result.verification is None
    assert runner.run_tests.call_count == 1


def test_unseen_replacement_stops_before_patching(
    repair_setup, monkeypatch
):
    workspace, _ = repair_setup
    runner = install_fake_runner(
        monkeypatch,
        maven_result(workspace.project_root, failed=True),
    )
    apply = Mock()
    monkeypatch.setattr(PatchApplier, "apply", apply)

    proposer = Mock(
        return_value=RepairProposal(
            explanation="Change a condition.",
            old_text="condition not present in the source",
            new_text="true",
        )
    )

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files={RELATIVE},
        required_tests=(REQUIRED,),
        propose_fix=proposer,
        production_path=RELATIVE,
        test_path=TEST_RELATIVE,
    )

    assert result.status is RepairAttemptStatus.PROPOSAL_FAILED
    apply.assert_not_called()
    assert result.verification is None
    assert runner.run_tests.call_count == 1