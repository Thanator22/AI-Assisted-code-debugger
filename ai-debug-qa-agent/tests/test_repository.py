from pathlib import Path
import pytest
from repo_doctor.tools.repository import Repository, RepositoryError


def test_resolve_path_inside_repository(tmp_path: Path):
    source = tmp_path / "src" / "App.java"
    source.parent.mkdir()
    source.write_text("class App {}", encoding="utf-8")

    repository = Repository(tmp_path)

    assert repository.resolve_path("src/App.java") == source.resolve()


def test_rejects_path_outside_repository(tmp_path: Path):
    repository = Repository(tmp_path)

    with pytest.raises(RepositoryError):
        repository.resolve_path("../secret.txt")


def test_reads_text_file(tmp_path: Path):
    source = tmp_path / "Example.java"
    source.write_text("class Example {}", encoding="utf-8")

    repository = Repository(tmp_path)

    assert repository.read_file("Example.java") == "class Example {}"


def test_rejects_missing_file(tmp_path: Path):
    repository = Repository(tmp_path)

    with pytest.raises(RepositoryError):
        repository.read_file("Missing.java")


def test_rejects_directory_as_file(tmp_path: Path):
    directory = tmp_path / "src"
    directory.mkdir()

    repository = Repository(tmp_path)

    with pytest.raises(RepositoryError):
        repository.read_file("src")
def test_lists_repository_files(tmp_path: Path):
    java_file = tmp_path / "src" / "App.java"
    java_file.parent.mkdir()
    java_file.write_text("class App {}", encoding="utf-8")

    pom_file = tmp_path / "pom.xml"
    pom_file.write_text("<project />", encoding="utf-8")

    repository = Repository(tmp_path)

    assert repository.list_files() == [
        "pom.xml",
        "src/App.java",
    ]


def test_filters_files_by_extension(tmp_path: Path):
    java_file = tmp_path / "App.java"
    java_file.write_text("class App {}", encoding="utf-8")

    text_file = tmp_path / "notes.txt"
    text_file.write_text("Notes", encoding="utf-8")

    repository = Repository(tmp_path)

    assert repository.list_files({".java"}) == ["App.java"]


def test_ignores_generated_directories(tmp_path: Path):
    source_file = tmp_path / "src" / "App.java"
    source_file.parent.mkdir()
    source_file.write_text("class App {}", encoding="utf-8")

    generated_file = tmp_path / "target" / "App.class"
    generated_file.parent.mkdir()
    generated_file.write_bytes(b"compiled")

    repository = Repository(tmp_path)

    assert repository.list_files() == ["src/App.java"]


def test_limits_number_of_files(tmp_path: Path):
    for index in range(10):
        (tmp_path / f"File{index}.java").write_text(
            f"class File{index} {{}}",
            encoding="utf-8",
        )

    repository = Repository(tmp_path)

    assert len(repository.list_files(limit=3)) == 3