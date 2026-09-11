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