from pathlib import Path

import pytest

from repo_doctor.tools.build import (
    BuildSystem,
    detect_build_system,
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