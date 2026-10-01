from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
import shutil


class WorkspaceError(Exception):
    """Raised when a repair workspace cannot be prepared."""


@dataclass(frozen=True)
class RepairWorkspace:
    source_root: Path
    project_root: Path


def create_fixture_workspace(
    source_root: str | Path,
) -> RepairWorkspace:
    source = Path(source_root).resolve()

    if not source.is_dir():
        raise WorkspaceError(
            f"Fixture directory does not exist: {source}"
        )

    # This helper deliberately supports our controlled fixture layout.
    for name in ("pom.xml", "bug-report.md"):
        path = source / name

        if path.is_symlink() or not path.is_file():
            raise WorkspaceError(
                f"Expected a regular fixture file: {name}"
            )

    source_directory = source / "src"

    if source_directory.is_symlink() or not source_directory.is_dir():
        raise WorkspaceError("Expected a regular src directory")

    for path in source_directory.rglob("*"):
        if path.is_symlink():
            raise WorkspaceError(
                f"Fixture contains a symbolic link: "
                f"{path.relative_to(source)}"
            )

        if not path.is_dir() and not path.is_file():
            raise WorkspaceError(
                f"Unsupported fixture entry: {path.relative_to(source)}"
            )

    destination = Path(
        mkdtemp(prefix="repo-doctor-repair-")
    ).resolve()

    try:
        for name in ("pom.xml", "bug-report.md"):
            shutil.copy2(source / name, destination / name)

        shutil.copytree(
            source_directory,
            destination / "src",
            symlinks=True,
        )
    except OSError as error:
        raise WorkspaceError(
            f"Workspace copy failed; partial workspace retained at "
            f"{destination}"
        ) from error

    return RepairWorkspace(
        source_root=source,
        project_root=destination,
    )