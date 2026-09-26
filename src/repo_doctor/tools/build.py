from enum import Enum
import os
import shutil
from pathlib import Path
from dataclasses import dataclass
from tempfile import mkdtemp

from repo_doctor.tools.process import (
    CommandResult,
    ProcessRunner,
)
from repo_doctor.tools.test_reports import (
    ReportCollectionResult,
    collect_surefire_reports,
)


class BuildSystem(str, Enum):
    MAVEN = "maven"
    GRADLE = "gradle"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"

def detect_build_system(project_root: Path) -> BuildSystem:
    resolved_root = project_root.resolve()

    if not resolved_root.is_dir():
        raise ValueError(
            f"Project root is not a directory: {resolved_root}"
        )

    has_maven = (
        resolved_root / "pom.xml"
    ).is_file()

    has_gradle = any(
        (
            (resolved_root / "build.gradle").is_file(),
            (resolved_root / "build.gradle.kts").is_file(),
        )
    )

    if has_maven and has_gradle:
        return BuildSystem.AMBIGUOUS

    if has_maven:
        return BuildSystem.MAVEN

    if has_gradle:
        return BuildSystem.GRADLE

    return BuildSystem.UNKNOWN

@dataclass(frozen=True)
class MavenTestResult:
    execution: CommandResult
    reports: ReportCollectionResult
    report_directory: Path

class MavenTestRunner:
    def __init__(
        self,
        project_root: Path,
        process_runner: ProcessRunner | None = None,
        timeout_seconds: float = 120.0,
    ):
        self.project_root = project_root.resolve()

        build_system = detect_build_system(
            self.project_root
        )

        if build_system is not BuildSystem.MAVEN:
            raise ValueError(
                "MavenTestRunner requires a Maven project, "
                f"but detected: {build_system.value}"
            )

        if timeout_seconds <= 0:
            raise ValueError(
                "Maven timeout must be greater than zero"
            )

        self.process_runner = (
            process_runner
            if process_runner is not None
            else ProcessRunner(self.project_root)
        )
        self.timeout_seconds = timeout_seconds

    def _resolve_maven_executable(self) -> str:
        wrapper_name = (
            "mvnw.cmd"
            if os.name == "nt"
            else "mvnw"
        )
        wrapper = self.project_root / wrapper_name

        if wrapper.is_file():
            return str(wrapper)

        return shutil.which("mvn") or "mvn"

    def run_tests(self) -> MavenTestResult:
        runs_directory = (
        self.project_root / "target" / "repo-doctor-runs"
        )
        runs_directory.mkdir(parents=True, exist_ok=True)

        report_directory = Path(
            mkdtemp(prefix="run-", dir=runs_directory)
        )

        executable = self._resolve_maven_executable()

        command = (
            executable,
            "--batch-mode",
            "--no-transfer-progress",
            (
                "-DrepoDoctor.reportsDirectory="
                + str(report_directory)
            ),
            "test",
        )

        execution = self.process_runner.run(
            command,
            working_directory=self.project_root,
            timeout_seconds=self.timeout_seconds,
        )

        reports = collect_surefire_reports(report_directory)

        return MavenTestResult(
            execution=execution,
            reports=reports,
            report_directory=report_directory,
        )