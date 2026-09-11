from pathlib import Path
from dataclasses import dataclass

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
    
class Repository:
    def __init__(self, root: str | Path):
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
        candidate = (self.root / relative_path).resolve()        
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
        
    def list_files(self, extensions: set[str] | None = None, limit: int = 500,) -> list[str]:
        """
        Return repository-relative file paths.    
        If extensions is provided, only files with those extensions
        should be included.
        """
        files = []

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

                # TODO 3: If query appears in the line,append a SearchMatch.
                if search_query in search_line:
                    matches.append(SearchMatch(
                        path=relative_path,
                        line_number=line_number,
                        line=line.rstrip(),
                    ))

                # TODO 4: Stop immediately once limit is reached.
                if len(matches) >= limit:
                    break
        return matches