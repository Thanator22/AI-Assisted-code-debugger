from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from io import StringIO
from stat import S_ISREG

IGNORED_DIRECTORIES = {
    ".git",
    ".idea",
    ".gradle",
    ".venv",
    "node_modules",
    "target",
    "build",
}

class RepositoryError(Exception):
    """Raised when a repository operation is unsafe or invalid."""

#data class for search match results, generates __init__, __repr__, __eq__, and other methods automatically
@dataclass(frozen=True) #frozen makes the dataclass immutable, which is good for value objects like SearchMatch
class SearchMatch: 
    path: str
    line_number: int
    line: str
    
class FileReadStatus(str, Enum):
    SUCCESS = "success"
    INVALID_PATH = "invalid_path"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    TOO_LARGE = "too_large"
    DECODE_ERROR = "decode_error"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class FileReadResult:
    status: FileReadStatus
    relative_path: str
    start_line: int
    end_line: int | None
    content: str
    truncated: bool
    diagnostic: str = ""

class Repository:
    def __init__(
        self,
        root: str | Path,
        *,
        max_file_bytes: int = 1_048_576,
        max_read_lines: int = 200,
        max_output_characters: int = 20_000,
    ):
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_read_lines", max_read_lines),
            ("max_output_characters", max_output_characters),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_file_bytes = max_file_bytes
        self.max_read_lines = max_read_lines
        self.max_output_characters = max_output_characters
        self.root = Path(root).resolve()

        if not self.root.exists():
            raise RepositoryError(
                f"Repository does not exist: {self.root}"
            )

        if not self.root.is_dir():
            raise RepositoryError(
                f"Repository path is not a directory: {self.root}"
            )

    def resolve_path(self, relative_path: str | Path) -> Path:
        """
        Resolve a repository-relative path.

        The resulting path must remain inside self.root.
        """
        requested = Path(relative_path)

        if requested.anchor:
            raise RepositoryError(
                "Path must be repository-relative"
            )

        candidate = (self.root / requested).resolve()

        if not candidate.is_relative_to(self.root):
            raise RepositoryError(
                f"Resolved path is outside the repository: {relative_path}"
            )

        return candidate

    def read_file(self, relative_path: str | Path) -> str:
        """
        Read a UTF-8 text file from the repository.
        """
        path = self.resolve_path(relative_path)
        if not path.exists():
            raise RepositoryError(f"File does not exist: {relative_path}")
        if not path.is_file():
            raise RepositoryError(f"Path is not a file: {relative_path}")
        try: 
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise RepositoryError(f"Failed to read file: {relative_path}") from e
        
    def read_lines(
        self,
        relative_path: str | Path,
        start_line: int = 1,
        end_line: int = 200,
    ) -> FileReadResult:
        """Read a bounded, 1-based inclusive range from a UTF-8 file.

        Preserve LF, CRLF and CR endings. EOF is not truncation. Output
        limits may return a partial final line; end_line includes that line.
        Empty results have end_line None. Failures return no partial content.
        Limits configured on Repository apply to this method only.
        """
        if type(start_line) is not int or type(end_line) is not int:
            raise ValueError("Line numbers must be integers")
        if start_line < 1 or end_line < start_line:
            raise ValueError("Require 1 <= start_line <= end_line")

        def failure(status: FileReadStatus, diagnostic: str) -> FileReadResult:
            return FileReadResult(
                status, str(relative_path), start_line, None, "", False, diagnostic
            )

        try:
            path = self.resolve_path(relative_path)
            if not S_ISREG(path.stat().st_mode):
                return failure(FileReadStatus.NOT_A_FILE, "Path is not a regular file")
            with path.open("rb") as source:
                # The extra byte detects oversize input, including a growing file.
                data = source.read(self.max_file_bytes + 1)
        except (RepositoryError, ValueError) as exc:
            return failure(FileReadStatus.INVALID_PATH, str(exc))
        except FileNotFoundError:
            return failure(FileReadStatus.NOT_FOUND, "File does not exist")
        except (IsADirectoryError, NotADirectoryError):
            return failure(FileReadStatus.NOT_A_FILE, "Path is not a regular file")
        except OSError as exc:
            return failure(FileReadStatus.READ_FAILED, f"Could not read file: {type(exc).__name__}")

        if len(data) > self.max_file_bytes:
            return failure(FileReadStatus.TOO_LARGE, "File exceeds max_file_bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return failure(FileReadStatus.DECODE_ERROR, "File is not valid UTF-8")

        chunks: list[str] = []
        remaining = self.max_output_characters
        last_line = None
        truncated = False
        # newline="" recognizes conventional source-file endings without changing them.
        with StringIO(text, newline="") as lines:
            for number, line in enumerate(lines, start=1):
                if number < start_line:
                    continue
                if number > end_line:
                    break
                if len(chunks) >= self.max_read_lines or remaining == 0:
                    truncated = True
                    break
                chunk = line[:remaining]
                chunks.append(chunk)
                last_line = number
                remaining -= len(chunk)
                if len(chunk) < len(line):
                    truncated = True
                    break

        return FileReadResult(
            FileReadStatus.SUCCESS, str(relative_path), start_line, last_line,
            "".join(chunks), truncated,
        )

    def list_files(self, extensions: set[str] | None = None, limit: int = 500,) -> list[str]:
        """
        Return repository-relative file paths.    
        If extensions is provided, only files with those extensions
        should be included.
        """
        files = []
        if limit <= 0:
            raise RepositoryError("Limit must be greater than zero")

        for path in self.root.rglob("*"):
            relative_path = path.relative_to(self.root)
            #Skip paths inside ignored directories.    
            if any(part in IGNORED_DIRECTORIES for part in relative_path.parts):
                continue
            #Skip anything that isn't a regular file.
            if not path.is_file():
                continue
            #If extensions was provided, check path.suffix.
            if extensions is not None and path.suffix not in extensions:
                continue
            #Add a portable string path to files.
            files.append(str(relative_path.as_posix()))

        files.sort()
        return files[:limit]
        
    def search_text(
        self, 
        query: str, 
        extensions: set[str] | None = None, 
        case_sensitive: bool = False, 
        limit: int = 50,) -> list[SearchMatch]:
        """
        Find lines containing query across repository files.
        """
        if not query:
            raise RepositoryError("Search query cannot be empty")
        if limit <= 0:
            raise RepositoryError("Limit must be greater than zero")
        matches: list[SearchMatch] = []
        files = self.list_files(
            extensions=extensions,
            limit=100_000,
        )
        for relative_path in files:
            # Read file, if the file cannot be decoded/read, skip it.
            try:
                content = self.read_file(relative_path)
            except RepositoryError:
                continue                

            for line_number, line in enumerate(
                content.splitlines(),
                start=1,
            ):
                # Prepare the query and line for comparison. The behavior depends on case_sensitive.
                if case_sensitive:
                    search_query = query
                    search_line  = line
                else:
                    search_query = query.lower()
                    search_line  = line.lower()
                    
                if search_query in search_line:
                    matches.append(SearchMatch(
                        path=relative_path,
                        line_number=line_number,
                        line=line.rstrip(),
                    ))
                    
                if len(matches) >= limit:
                    return matches
        return matches
