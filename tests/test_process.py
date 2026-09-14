import pytest
import sys
from pathlib import Path

from repo_doctor.tools.process import (
    CommandResult,
    CommandStatus,
    ProcessRunner,
)


def test_command_result_preserves_execution_details():
    result = CommandResult(
        status=CommandStatus.SUCCESS,
        command=("python", "--version"),
        working_directory=".",
        exit_code=0,
        stdout="Python 3",
        stderr="",
        duration_seconds=0.1,
        output_truncated=False,
    )

    assert result.status is CommandStatus.SUCCESS
    assert result.command == ("python", "--version")
    assert result.exit_code == 0
    assert result.stdout == "Python 3"
    assert result.output_truncated is False


def test_command_result_is_immutable():
    result = CommandResult(
        status=CommandStatus.SUCCESS,
        command=("python", "--version"),
        working_directory=".",
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1,
        output_truncated=False,
    )

    with pytest.raises(Exception):
        result.exit_code = 1
def test_runs_successful_command(tmp_path: Path):
    runner = ProcessRunner(tmp_path)

    result = runner.run(
        (
            sys.executable,
            "-c",
            "print('hello from child process')",
        )
    )

    assert result.status is CommandStatus.SUCCESS
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello from child process"
    assert result.stderr == ""
    assert result.duration_seconds >= 0


def test_captures_non_zero_exit(tmp_path: Path):
    runner = ProcessRunner(tmp_path)

    result = runner.run(
        (
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        )
    )

    assert result.status is CommandStatus.NON_ZERO_EXIT
    assert result.exit_code == 7


def test_reports_missing_executable(tmp_path: Path):
    runner = ProcessRunner(tmp_path)

    result = runner.run(
        ("repo-doctor-executable-that-does-not-exist",)
    )

    assert result.status is CommandStatus.EXECUTABLE_NOT_FOUND
    assert result.exit_code is None


def test_rejects_working_directory_outside_root(
    tmp_path: Path,
):
    runner = ProcessRunner(tmp_path)

    result = runner.run(
        (sys.executable, "--version"),
        working_directory=tmp_path.parent,
    )

    assert (
        result.status
        is CommandStatus.INVALID_WORKING_DIRECTORY
    )
    assert result.exit_code is None
    
def test_times_out_long_running_command(tmp_path: Path):
    runner = ProcessRunner(
        tmp_path,
        default_timeout_seconds=0.1,
    )

    result = runner.run(
        (
            sys.executable,
            "-c",
            (
                "import time; "
                "print('started', flush=True); "
                "time.sleep(5)"
            ),
        )
    )

    assert result.status is CommandStatus.TIMED_OUT
    assert result.exit_code is None
    assert "started" in result.stdout
    assert result.duration_seconds >= 0.1


def test_allows_timeout_override(tmp_path: Path):
    runner = ProcessRunner(
        tmp_path,
        default_timeout_seconds=10,
    )

    result = runner.run(
        (
            sys.executable,
            "-c",
            "print('completed')",
        ),
        timeout_seconds=1,
    )

    assert result.status is CommandStatus.SUCCESS
    assert result.stdout.strip() == "completed"


def test_rejects_non_positive_timeout(tmp_path: Path):
    runner = ProcessRunner(tmp_path)

    with pytest.raises(
        ValueError,
        match="Timeout must be greater than zero",
    ):
        runner.run(
            (sys.executable, "--version"),
            timeout_seconds=0,
        )
        
def test_truncates_large_stdout(tmp_path: Path):
    runner = ProcessRunner(
        tmp_path,
        max_output_characters=100,
    )

    result = runner.run(
        (
            sys.executable,
            "-c",
            "print('A' * 500)",
        )
    )

    assert result.status is CommandStatus.SUCCESS
    assert result.output_truncated is True
    assert len(result.stdout) <= 100
    assert "<output truncated>" in result.stdout


def test_does_not_mark_small_output_as_truncated(
    tmp_path: Path,
):
    runner = ProcessRunner(
        tmp_path,
        max_output_characters=100,
    )

    result = runner.run(
        (
            sys.executable,
            "-c",
            "print('small output')",
        )
    )

    assert result.output_truncated is False
    assert result.stdout.strip() == "small output"


def test_rejects_non_positive_output_limit(
    tmp_path: Path,
):
    with pytest.raises(
        ValueError,
        match="Maximum output size must be greater than zero",
    ):
        ProcessRunner(
            tmp_path,
            max_output_characters=0,
        )
        
def test_rejects_empty_command(tmp_path: Path):
    runner = ProcessRunner(tmp_path)

    with pytest.raises(
        ValueError,
        match="Command must contain an executable",
    ):
        runner.run(())


def test_reports_process_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runner = ProcessRunner(tmp_path)

    def deny_process_start(*args, **kwargs):
        raise PermissionError("Access denied")

    monkeypatch.setattr(
        "repo_doctor.tools.process.subprocess.run",
        deny_process_start,
    )

    result = runner.run(("blocked-command",))

    assert result.status is CommandStatus.START_FAILED
    assert result.exit_code is None
    assert "Access denied" in result.stderr