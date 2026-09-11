from pathlib import Path

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