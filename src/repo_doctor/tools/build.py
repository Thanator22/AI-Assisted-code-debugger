from enum import Enum
from pathlib import Path


class BuildSystem(str, Enum):
    MAVEN = "maven"
    GRADLE = "gradle"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


def detect_build_system(
    project_root: Path,
) -> BuildSystem:
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