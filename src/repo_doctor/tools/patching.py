from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from hashlib import sha256
from stat import S_ISREG, S_IMODE
import os
from tempfile import NamedTemporaryFile

from repo_doctor.tools.repository import Repository, RepositoryError
from repo_doctor.tools.workspace import RepairWorkspace

class PatchStatus(str, Enum):
    APPLIED = "applied"
    INVALID_PATH = "invalid_path"
    NOT_ALLOWED = "not_allowed"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    TOO_LARGE = "too_large"
    DECODE_ERROR = "decode_error"
    STALE_CONTENT = "stale_content"
    MATCH_NOT_FOUND = "match_not_found"
    AMBIGUOUS_MATCH = "ambiguous_match"
    READ_FAILED = "read_failed"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class PatchResult:
    status: PatchStatus
    relative_path: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    diagnostic: str = ""
    
@dataclass(frozen=True)
class ReplacementRequest:
    relative_path: str
    expected_sha256: str
    old_text: str
    new_text: str

    def __post_init__(self) -> None:
        if not self.old_text:
            raise ValueError("Old text cannot be empty")

        if self.old_text == self.new_text:
            raise ValueError("Replacement must change the text")
        
@dataclass(frozen=True)
class _PreparedReplacement:
    path: Path
    before: bytes
    after: bytes
    before_sha256: str
    after_sha256: str
        
class PatchApplier:
    def __init__(
        self,
        workspace: RepairWorkspace,
        allowed_files: set[str],
        max_file_bytes: int = 1_000_000,
    ):
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError(
                "Maximum file size must be a positive integer"
            )

        source = workspace.source_root.resolve()
        destination = workspace.project_root.resolve()

        # Refuse a workspace that overlaps the original fixture.
        if (
            destination.is_relative_to(source)
            or source.is_relative_to(destination)
        ):
            raise ValueError(
                "Repair workspace must be separate from the source"
            )

        self.repository = Repository(destination)
        self.max_file_bytes = max_file_bytes

        approved: set[Path] = set()

        for relative_path in allowed_files:
            target = self.repository.resolve_path(relative_path)
            canonical_relative = target.relative_to(
                self.repository.root
            )

            # First version: existing Java production files only.
            if (
                canonical_relative.parts[:3]
                != ("src", "main", "java")
                or target.suffix != ".java"
            ):
                raise ValueError(
                    f"Only Java production files can be approved: "
                    f"{relative_path}"
                )

            approved.add(target)

        self.allowed_paths = frozenset(approved)
        
    def apply(self, request: ReplacementRequest) -> PatchResult:
        prepared = self._prepare(request)

        if isinstance(prepared, PatchResult):
            return prepared

        temporary_path: Path | None = None

        try:
            original_mode = S_IMODE(prepared.path.stat().st_mode)

            with NamedTemporaryFile(
                mode="wb",
                dir=prepared.path.parent,
                prefix=".repo-doctor-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(prepared.after)

            # The temporary file is closed before replacement, including on Windows.
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, prepared.path)

        except OSError as error:
            diagnostic = (
                f"Could not apply replacement: {type(error).__name__}"
            )

            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    diagnostic += (
                        f"; temporary file cleanup failed: "
                        f"{temporary_path.name}"
                    )

            return PatchResult(
                status=PatchStatus.WRITE_FAILED,
                relative_path=request.relative_path,
                before_sha256=prepared.before_sha256,
                diagnostic=diagnostic,
            )

        return PatchResult(
            status=PatchStatus.APPLIED,
            relative_path=request.relative_path,
            before_sha256=prepared.before_sha256,
            after_sha256=prepared.after_sha256,
        )

    def _resolve_target(
        self,
        relative_path: str,
    ) -> Path | PatchResult:
        try:
            target = self.repository.resolve_path(relative_path)
        except (RepositoryError, ValueError, RuntimeError):
            return PatchResult(
                status=PatchStatus.INVALID_PATH,
                relative_path=relative_path,
                diagnostic="Path cannot be resolved within the workspace",
            )
        except OSError:
            return PatchResult(
                status=PatchStatus.READ_FAILED,
                relative_path=relative_path,
                diagnostic="Could not resolve the target path",
            )

        if target not in self.allowed_paths:
            return PatchResult(
                status=PatchStatus.NOT_ALLOWED,
                relative_path=relative_path,
                diagnostic="File is not approved for editing",
            )

        return target
    
    def _prepare(
        self,
        request: ReplacementRequest,
    ) -> _PreparedReplacement | PatchResult:
        target = self._resolve_target(request.relative_path)

        if isinstance(target, PatchResult):
            return target

        before_hash: str | None = None

        def reject(status: PatchStatus, message: str) -> PatchResult:
            return PatchResult(
                status=status,
                relative_path=request.relative_path,
                before_sha256=before_hash,
                diagnostic=message,
            )

        try:
            if not S_ISREG(target.stat().st_mode):
                return reject(
                    PatchStatus.NOT_A_FILE,
                    "Target is not a regular file",
                )

            with target.open("rb") as source:
                before = source.read(self.max_file_bytes + 1)

        except FileNotFoundError:
            return reject(PatchStatus.NOT_FOUND, "Target does not exist")
        except (IsADirectoryError, NotADirectoryError):
            return reject(
                PatchStatus.NOT_A_FILE,
                "Target is not a regular file",
            )
        except OSError:
            return reject(PatchStatus.READ_FAILED, "Could not read target")

        if len(before) > self.max_file_bytes:
            return reject(
                PatchStatus.TOO_LARGE,
                "Target exceeds the file-size limit",
            )

        before_hash = sha256(before).hexdigest()

        if before_hash != request.expected_sha256:
            return reject(
                PatchStatus.STALE_CONTENT,
                "File differs from the inspected version; read it again",
            )

        try:
            before.decode("utf-8")
            old = request.old_text.encode("utf-8")
            new = request.new_text.encode("utf-8")
        except UnicodeError:
            return reject(
                PatchStatus.DECODE_ERROR,
                "File or replacement text is not valid UTF-8",
            )

        first = before.find(old)

        if first == -1:
            return reject(
                PatchStatus.MATCH_NOT_FOUND,
                "Expected text was not found",
            )

        # Start one byte later to detect overlapping matches too.
        if before.find(old, first + 1) != -1:
            return reject(
                PatchStatus.AMBIGUOUS_MATCH,
                "Expected text occurs more than once",
            )

        resulting_size = len(before) - len(old) + len(new)

        if resulting_size > self.max_file_bytes:
            return reject(
                PatchStatus.TOO_LARGE,
                "Replacement would exceed the file-size limit",
            )

        after = before[:first] + new + before[first + len(old):]

        return _PreparedReplacement(
            path=target,
            before=before,
            after=after,
            before_sha256=before_hash,
            after_sha256=sha256(after).hexdigest(),
        )