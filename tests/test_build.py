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
    detect_build_system,
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

    assert result is expected_result

    fake_process_runner.run.assert_called_once_with(
        (
            "/tools/mvn",
            "--batch-mode",
            "--no-transfer-progress",
            "test",
        ),
        working_directory=".",
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