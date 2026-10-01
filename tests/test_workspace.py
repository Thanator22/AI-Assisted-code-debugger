from pathlib import Path

import pytest

from repo_doctor.tools.workspace import (
    WorkspaceError,
    create_fixture_workspace,
)


def make_fixture(root: Path) -> Path:
    source = root / "fixture"
    source.mkdir()

    (source / "pom.xml").write_text(
        "<project/>",
        encoding="utf-8",
    )
    (source / "bug-report.md").write_text(
        "Discount is missing at the threshold.",
        encoding="utf-8",
    )

    java_directory = source / "src" / "main" / "java"
    java_directory.mkdir(parents=True)
    (java_directory / "Example.java").write_text(
        "original",
        encoding="utf-8",
    )

    return source


def test_workspace_copies_inputs_and_excludes_generated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = make_fixture(tmp_path)

    generated = source / "target"
    generated.mkdir()
    (generated / "old-report.xml").write_text(
        "stale",
        encoding="utf-8",
    )

    # Keep test-created workspaces inside pytest's temporary directory.
    destination = tmp_path / "repair"

    def make_test_directory(**kwargs):
        destination.mkdir()
        return str(destination)

    monkeypatch.setattr(
        "repo_doctor.tools.workspace.mkdtemp",
        make_test_directory,
    )

    workspace = create_fixture_workspace(source)

    assert workspace.source_root == source.resolve()
    assert workspace.project_root == destination.resolve()
    assert (destination / "pom.xml").is_file()
    assert (destination / "bug-report.md").is_file()
    assert not (destination / "target").exists()

    copied_java = destination / "src/main/java/Example.java"
    assert copied_java.read_text(encoding="utf-8") == "original"

    copied_java.write_text("modified", encoding="utf-8")

    original_java = source / "src/main/java/Example.java"
    assert original_java.read_text(encoding="utf-8") == "original"


def test_workspace_rejects_incomplete_fixture(tmp_path: Path):
    with pytest.raises(
        WorkspaceError,
        match="regular fixture file",
    ):
        create_fixture_workspace(tmp_path)
        
