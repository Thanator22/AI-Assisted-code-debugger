from pathlib import Path
import pytest
from repo_doctor.tools.repository import (
    FileReadStatus,
    Repository,
    RepositoryError,
    SearchMatch,
)

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
    
def test_searches_repository_text(tmp_path: Path):
    source = tmp_path / "UserService.java"
    source.write_text("""
        public class UserService {
    throw new UserNotFoundException();
        }""".strip(), encoding="utf-8",
    )

    repository = Repository(tmp_path)

    assert repository.search_text(
        "UserNotFoundException"
    ) == [
        SearchMatch(
            path="UserService.java",
            line_number=2,
            line="    throw new UserNotFoundException();",  # Match the spaces indentation
        )
    ]


def test_search_is_case_insensitive_by_default(tmp_path: Path):
    source = tmp_path / "UserService.java"
    source.write_text(
        "class UserService {}",
        encoding="utf-8",
    )

    repository = Repository(tmp_path)

    matches = repository.search_text("userservice")

    assert len(matches) == 1
    assert matches[0].line_number == 1

def test_supports_case_sensitive_search(tmp_path: Path):
    source = tmp_path / "UserService.java"
    source.write_text(
        "class UserService {}",
        encoding="utf-8",
    )

    repository = Repository(tmp_path)

    matches = repository.search_text(
        "userservice",
        case_sensitive=True,
    )

    assert matches == []

def test_search_filters_extensions(tmp_path: Path):
    java_file = tmp_path / "UserService.java"
    java_file.write_text("findUser", encoding="utf-8")

    text_file = tmp_path / "notes.txt"
    text_file.write_text("findUser", encoding="utf-8")

    repository = Repository(tmp_path)

    matches = repository.search_text(
        "findUser",
        extensions={".java"},
    )

    assert [match.path for match in matches] == [
        "UserService.java"
    ]

def test_search_respects_result_limit(tmp_path: Path):
    source = tmp_path / "Repeated.java"
    source.write_text(
        "match\nmatch\nmatch\nmatch",
        encoding="utf-8",
    )

    repository = Repository(tmp_path)

    matches = repository.search_text("match", limit=2)

    assert len(matches) == 2
    assert matches[0].line_number == 1
    assert matches[1].line_number == 2

def test_rejects_empty_search_query(tmp_path: Path):
    repository = Repository(tmp_path)

    with pytest.raises(RepositoryError):
        repository.search_text("")


def test_search_limit_applies_across_files(tmp_path: Path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("match", encoding="utf-8")
    assert Repository(tmp_path).search_text("match", limit=1) == [
        SearchMatch("a.txt", 1, "match")
    ]


@pytest.mark.parametrize("limit", [0, -1])
def test_rejects_nonpositive_limits_before_listing(tmp_path: Path, monkeypatch, limit):
    repository = Repository(tmp_path)
    with pytest.raises(RepositoryError):
        repository.list_files(limit=limit)

    def unexpected_listing(*args, **kwargs):
        pytest.fail("Invalid search limits should be rejected before listing")

    monkeypatch.setattr(repository, "list_files", unexpected_listing)
    with pytest.raises(RepositoryError):
        repository.search_text("match", limit=limit)


def test_rejects_absolute_path_inside_repository(tmp_path: Path):
    with pytest.raises(RepositoryError):
        Repository(tmp_path).resolve_path(tmp_path / "file.txt")


@pytest.mark.parametrize(
    "data,start,end,expected,last",
    [
        (b"one\r\ntwo\r\nthree", 2, 3, "two\r\nthree", 3),
        (b"one\ntwo\nthree\n", 2, 20, "two\nthree\n", 3),
        (b"one\rtwo\rthree", 2, 2, "two\r", 2),
        (b"", 1, 2, "", None),
        (b"one\n", 2, 3, "", None),
    ],
)
def test_read_lines_ranges(tmp_path: Path, data, start, end, expected, last):
    (tmp_path / "file.txt").write_bytes(data)
    result = Repository(tmp_path).read_lines("file.txt", start, end)
    assert result.status == FileReadStatus.SUCCESS
    assert result.relative_path == "file.txt"
    assert result.start_line == start
    assert result.end_line == last
    assert result.content == expected
    assert not result.truncated
    assert result.diagnostic == ""


@pytest.mark.parametrize("start,end", [(0, 1), (-1, 2), (3, 2), (1.5, 2), (1, True)])
def test_read_lines_rejects_invalid_ranges(tmp_path: Path, start, end):
    with pytest.raises(ValueError):
        Repository(tmp_path).read_lines("missing.txt", start, end)


@pytest.mark.parametrize("name", ["max_file_bytes", "max_read_lines", "max_output_characters"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_rejects_invalid_read_limits(tmp_path: Path, name, value):
    with pytest.raises(ValueError):
        Repository(tmp_path, **{name: value})


@pytest.mark.parametrize(
    "data,limit,status",
    [(b"abcd", 4, FileReadStatus.SUCCESS),
     (b"abcde", 4, FileReadStatus.TOO_LARGE),
     (b"\xff", 4, FileReadStatus.DECODE_ERROR)],
)
def test_read_lines_byte_limit_and_encoding(tmp_path: Path, data, limit, status):
    (tmp_path / "file.txt").write_bytes(data)
    result = Repository(tmp_path, max_file_bytes=limit).read_lines("file.txt")
    assert result.status == status
    if status != FileReadStatus.SUCCESS:
        assert result.content == ""
        assert result.end_line is None
        assert not result.truncated
        assert result.diagnostic


@pytest.mark.parametrize(
    "text,limits,end,expected,last,truncated",
    [
        ("a\nb\nc\n", {"max_read_lines": 2}, 10, "a\nb\n", 2, True),
        ("a\nb\n", {"max_read_lines": 2}, 10, "a\nb\n", 2, False),
        ("a\nb\nc\n", {"max_read_lines": 2}, 2, "a\nb\n", 2, False),
        ("abcdef", {"max_output_characters": 3}, 10, "abc", 1, True),
        ("a\nb\n", {"max_output_characters": 2}, 10, "a\n", 1, True),
        ("a\n", {"max_output_characters": 2}, 10, "a\n", 1, False),
        ("a\nb\n", {"max_output_characters": 2}, 1, "a\n", 1, False),
        ("é🙂z", {"max_output_characters": 2}, 10, "é🙂", 1, True),
    ],
)
def test_read_lines_output_limits(tmp_path: Path, text, limits, end, expected, last, truncated):
    (tmp_path / "file.txt").write_bytes(text.encode("utf-8"))
    result = Repository(tmp_path, **limits).read_lines("file.txt", 1, end)
    assert result.status == FileReadStatus.SUCCESS
    assert result.content == expected
    assert result.end_line == last
    assert result.truncated is truncated


@pytest.mark.parametrize(
    "requested,status",
    [("missing.txt", FileReadStatus.NOT_FOUND),
     (".", FileReadStatus.NOT_A_FILE),
     ("../outside.txt", FileReadStatus.INVALID_PATH),
     ("bad\x00path", FileReadStatus.INVALID_PATH)],
)
def test_read_lines_path_failures(tmp_path: Path, requested, status):
    result = Repository(tmp_path).read_lines(requested)
    assert result.status == status
    assert result.content == ""
    assert result.end_line is None
    assert result.diagnostic


def test_read_lines_absolute_path_is_invalid(tmp_path: Path):
    result = Repository(tmp_path).read_lines(tmp_path / "file.txt")
    assert result.status == FileReadStatus.INVALID_PATH


def test_read_lines_read_failure(tmp_path: Path, monkeypatch):
    (tmp_path / "file.txt").write_bytes(b"text")

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", denied)
    result = Repository(tmp_path).read_lines("file.txt")
    assert result.status == FileReadStatus.READ_FAILED
    assert result.content == ""


def test_read_lines_bounds_actual_read(tmp_path: Path, monkeypatch):
    from io import BytesIO

    # The file can grow after stat; the stream, not stat size, is authoritative.
    (tmp_path / "file.txt").write_bytes(b"")
    sizes = []

    class RecordingStream(BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: RecordingStream(b"abcdef"))
    result = Repository(tmp_path, max_file_bytes=4).read_lines("file.txt")
    assert sizes == [5]
    assert result.status == FileReadStatus.TOO_LARGE


def test_read_lines_symlink_containment(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "inside.txt").write_bytes(b"inside")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    try:
        (root / "internal-link.txt").symlink_to(root / "inside.txt")
        (root / "external-link.txt").symlink_to(outside)
    except OSError as exc:
        if exc.errno in (1, 13) or getattr(exc, "winerror", None) == 1314:
            pytest.skip("Creating symlinks requires platform permission")
        raise
    repository = Repository(root)
    assert repository.read_lines("internal-link.txt").content == "inside"
    assert repository.read_lines("external-link.txt").status == FileReadStatus.INVALID_PATH
