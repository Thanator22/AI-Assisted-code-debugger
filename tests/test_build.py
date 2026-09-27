from pathlib import Path
import os
from unittest.mock import Mock

from repo_doctor.tools.build import MavenTestRunner
from repo_doctor.tools.process import (
    CommandResult,
    CommandStatus,
    ProcessRunner,
)
import pytest

from repo_doctor.tools.build import (
    BuildSystem,
    MavenTestResult,
    TestRunOutcome as Outcome,
    classify_test_run,
    detect_build_system,
)
from repo_doctor.tools.test_reports import (
    ReportCollectionResult,
    ReportCollectionStatus,
    TestReport as Report,
    TestIssue as Issue,
    TestIssueKind as IssueKind,
)
def successful_command_result() -> CommandResult:
    return CommandResult(
        status=CommandStatus.SUCCESS,
        command=("mvn", "test"),
        working_directory=".",
        exit_code=0,
        stdout="BUILD SUCCESS",
        stderr="",
        duration_seconds=1.0,
        output_truncated=False,
    )


def test_detects_maven_project(tmp_path: Path):
    (tmp_path / "pom.xml").write_text(
        "<project></project>",
        encoding="utf-8",
    )

    result = detect_build_system(tmp_path)

    assert result is BuildSystem.MAVEN


def test_detects_gradle_project(tmp_path: Path):
    (tmp_path / "build.gradle").write_text(
        "",
        encoding="utf-8",
    )

    result = detect_build_system(tmp_path)

    assert result is BuildSystem.GRADLE


def test_detects_gradle_kotlin_project(tmp_path: Path):
    (tmp_path / "build.gradle.kts").write_text(
        "",
        encoding="utf-8",
    )

    result = detect_build_system(tmp_path)

    assert result is BuildSystem.GRADLE


def test_reports_unknown_build_system(tmp_path: Path):
    result = detect_build_system(tmp_path)

    assert result is BuildSystem.UNKNOWN


def test_reports_ambiguous_build_system(tmp_path: Path):
    (tmp_path / "pom.xml").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "build.gradle").write_text(
        "",
        encoding="utf-8",
    )

    result = detect_build_system(tmp_path)

    assert result is BuildSystem.AMBIGUOUS


def test_rejects_invalid_project_root(tmp_path: Path):
    missing_directory = tmp_path / "missing"

    with pytest.raises(
        ValueError,
        match="Project root is not a directory",
    ):
        detect_build_system(missing_directory)
        
def test_maven_runner_delegates_to_process_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / "pom.xml").write_text(
        "<project></project>",
        encoding="utf-8",
    )

    fake_process_runner = Mock(spec=ProcessRunner)
    expected_result = successful_command_result()
    fake_process_runner.run.return_value = expected_result

    monkeypatch.setattr(
        "repo_doctor.tools.build.shutil.which",
        lambda executable: "/tools/mvn",
    )

    runner = MavenTestRunner(
        tmp_path,
        process_runner=fake_process_runner,
        timeout_seconds=45,
    )

    result = runner.run_tests()

    assert result.execution is expected_result
    assert result.report_directory.is_dir()

    # The mock never runs Maven, so no reports are generated.
    assert result.reports.status is ReportCollectionStatus.MISSING
    assert result.reports.report is None

    fake_process_runner.run.assert_called_once_with(
        (
            "/tools/mvn",
            "--batch-mode",
            "--no-transfer-progress",
            (
                "-DrepoDoctor.reportsDirectory="
                + str(result.report_directory)
            ),
            "test",
        ),
        working_directory=tmp_path.resolve(),
        timeout_seconds=45,
    )


def test_maven_runner_prefers_project_wrapper(
    tmp_path: Path,
):
    (tmp_path / "pom.xml").write_text(
        "<project></project>",
        encoding="utf-8",
    )

    wrapper_name = (
        "mvnw.cmd"
        if os.name == "nt"
        else "mvnw"
    )
    wrapper = tmp_path / wrapper_name
    wrapper.write_text("", encoding="utf-8")

    fake_process_runner = Mock(spec=ProcessRunner)
    fake_process_runner.run.return_value = (
        successful_command_result()
    )

    runner = MavenTestRunner(
        tmp_path,
        process_runner=fake_process_runner,
    )

    runner.run_tests()

    executed_command = (
        fake_process_runner.run.call_args.args[0]
    )

    assert executed_command[0] == str(wrapper.resolve())


def test_maven_runner_rejects_non_maven_project(
    tmp_path: Path,
):
    with pytest.raises(
        ValueError,
        match="requires a Maven project",
    ):
        MavenTestRunner(tmp_path)

def test_maven_runs_use_separate_report_directories(tmp_path: Path):
    (tmp_path / "pom.xml").write_text(
        "<project/>",
        encoding="utf-8",
    )

    fake_process_runner = Mock(spec=ProcessRunner)
    fake_process_runner.run.return_value = successful_command_result()

    runner = MavenTestRunner(
        tmp_path,
        process_runner=fake_process_runner,
    )

    first = runner.run_tests()

    # Simulate a report left behind by the first execution.
    (first.report_directory / "TEST-old.xml").write_text(
        '<testsuite tests="0" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )

    second = runner.run_tests()

    assert first.report_directory != second.report_directory
    assert second.reports.status is ReportCollectionStatus.MISSING
    assert second.reports.report is None
    
def test_classifies_timeout_with_two_passing_tests_as_incomplete():
    execution = CommandResult(
        status=CommandStatus.TIMED_OUT,
        command=("mvn", "test"),
        working_directory=".",
        exit_code=None,
        stdout="Tests run: 2, Failures: 0, Errors: 0, Skipped: 0",
        stderr="",
        duration_seconds=45.0,
        output_truncated=False,
    )
    reports = ReportCollectionResult(
        status=ReportCollectionStatus.AVAILABLE,
        report=Report(
            total_tests=2,
            failures=0,
            errors=0,
            skipped=0,
            issues=(),
        ),
    )
    result = MavenTestResult(
        execution=execution,
        reports=reports,
        report_directory=Path("."),
    )

    assert classify_test_run(result) is Outcome.INCOMPLETE

def test_classifies_build_failed_with_four_passing_tests_as_build_failed():
    execution = CommandResult(
        status=CommandStatus.NON_ZERO_EXIT,
        command=("mvn", "test"),
        working_directory=".",
        exit_code=1,
        stdout="Tests run: 4, Failures: 0, Errors: 0, Skipped: 0",
        stderr="",
        duration_seconds=45.0,
        output_truncated=False,
    )
    reports = ReportCollectionResult(
        status=ReportCollectionStatus.AVAILABLE,
        report=Report(
            total_tests=4,
            failures=0,
            errors=0,
            skipped=0,
            issues=(),
        ),
    )
    result = MavenTestResult(
        execution=execution,
        reports=reports,
        report_directory=Path("."),
    )

    assert classify_test_run(result) is Outcome.BUILD_FAILED

def test_classifies_success_with_tests_skipped_as_no_tests_executed():
    execution = CommandResult(
        status=CommandStatus.SUCCESS,
        command=("mvn", "test"),
        working_directory=".",
        exit_code=0,
        stdout="Tests run: 4, Failures: 0, Errors: 0, Skipped: 4",
        stderr="",
        duration_seconds=45.0,
        output_truncated=False,
    )
    reports = ReportCollectionResult(
        status=ReportCollectionStatus.AVAILABLE,
        report=Report(
            total_tests=4,
            failures=0,
            errors=0,
            skipped=4,
            issues=(),
        ),
    )
    result = MavenTestResult(
        execution=execution,
        reports=reports,
        report_directory=Path("."),
    )

    assert classify_test_run(result) is Outcome.NO_TESTS_EXECUTED
    
def test_classifies_success_with_four_passing_tests_as_passed():
    execution = CommandResult(
        status=CommandStatus.SUCCESS,
        command=("mvn", "test"),
        working_directory=".",
        exit_code=0,
        stdout="Tests run: 4, Failures: 0, Errors: 0, Skipped: 0",
        stderr="",
        duration_seconds=45.0,
        output_truncated=False,
    )
    reports = ReportCollectionResult(
        status=ReportCollectionStatus.AVAILABLE,
        report=Report(
            total_tests=4,
            failures=0,
            errors=0,
            skipped=0,
            issues=(),
        ),
    )
    result = MavenTestResult(
        execution=execution,
        reports=reports,
        report_directory=Path("."),
    )

    assert classify_test_run(result) is Outcome.PASSED
    
@pytest.mark.parametrize(
    "command_status, exit_code",
    [
        (CommandStatus.NON_ZERO_EXIT, 1),
        (CommandStatus.SUCCESS, 0),
    ],
)
def test_reported_failure_prevents_passed_outcome(
    command_status,
    exit_code,
):
    execution = CommandResult(
        status=command_status,
        command=("mvn", "test"),
        working_directory=".",
        exit_code=exit_code,
        stdout="",
        stderr="",
        duration_seconds=1.0,
        output_truncated=False,
    )

    issue = Issue(
        test_name="appliesDiscountAtThreshold",
        class_name="com.example.discount.DiscountServiceTest",
        kind=IssueKind.FAILURE,
        message="expected: <900> but was: <1000>",
        exception_type="org.opentest4j.AssertionFailedError",
        stack_trace="at DiscountServiceTest.java:30",
    )

    reports = ReportCollectionResult(
        status=ReportCollectionStatus.AVAILABLE,
        report=Report(
            total_tests=4,
            failures=1,
            errors=0,
            skipped=0,
            issues=(issue,),
        ),
    )

    result = MavenTestResult(
        execution=execution,
        reports=reports,
        report_directory=Path("."),
    )

    assert classify_test_run(result) is Outcome.TESTS_FAILED
