import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CommandStatus(str, Enum):
    SUCCESS = "success"
    NON_ZERO_EXIT = "non_zero_exit"
    TIMED_OUT = "timed_out"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    INVALID_WORKING_DIRECTORY = "invalid_working_directory"
    START_FAILED = "start_failed"


@dataclass(frozen=True)
class CommandResult:
    status: CommandStatus
    command: tuple[str, ...]
    working_directory: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    output_truncated: bool
    

class ProcessRunner:
    def __init__(
        self,
        root: Path,
        default_timeout_seconds: float = 30.0,
        max_output_characters: int = 50_000,
    ):
        self.root = root.resolve()

        if not self.root.is_dir():
            raise ValueError(
                f"Process root is not a directory: {self.root}"
            )
        if default_timeout_seconds <= 0:
            raise ValueError(
                "Default timeout must be greater than zero"
            )
        if max_output_characters <= 0:
            raise ValueError(
                "Maximum output size must be greater than zero"
            )

        self.max_output_characters = max_output_characters
        self.default_timeout_seconds = default_timeout_seconds
    
    @staticmethod
    def _to_text(output: str | bytes | None) -> str:
        if output is None:
            return ""

        if isinstance(output, bytes):
            return output.decode(
                encoding="utf-8",
                errors="replace",
            )

        return output

    def _resolve_working_directory(
        self,
        working_directory: str | Path,
    ) -> Path | None:
        candidate = (self.root / working_directory).resolve()

        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None

        if not candidate.is_dir():
            return None

        return candidate

    def run(
        self,
        command: tuple[str, ...],
        working_directory: str | Path = ".",
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        resolved_directory = self._resolve_working_directory(
            working_directory
        ) 
        
        if not command or not command[0].strip():
                raise ValueError(
                "Command must contain an executable"
            )

        if resolved_directory is None:
            return CommandResult(
                status=CommandStatus.INVALID_WORKING_DIRECTORY,
                command=command,
                working_directory=str(working_directory),
                exit_code=None,
                stdout="",
                stderr="Working directory is invalid or outside the process root",
                duration_seconds=0.0,
                output_truncated=False,
            )
        effective_timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )

        if effective_timeout <= 0:
            raise ValueError(
                "Timeout must be greater than zero"
            )

        started_at = time.perf_counter()

        try:
            completed = subprocess.run(
                command,
                cwd=resolved_directory,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=effective_timeout,
            )
        except FileNotFoundError:
            duration = time.perf_counter() - started_at

            return CommandResult(
                status=CommandStatus.EXECUTABLE_NOT_FOUND,
                command=command,
                working_directory=str(resolved_directory),
                exit_code=None,
                stdout="",
                stderr=f"Executable not found: {command[0]}",
                duration_seconds=duration,
                output_truncated=False,
            )
        except OSError as error:
            duration = time.perf_counter() - started_at

            return CommandResult(
                status=CommandStatus.START_FAILED,
                command=command,
                working_directory=str(resolved_directory),
                exit_code=None,
                stdout="",
                stderr=f"Failed to start command: {error}",
                duration_seconds=duration,
                output_truncated=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout, stdout_truncated = self._truncate_output(
                self._to_text(error.stdout)
            )
            stderr, stderr_truncated = self._truncate_output(
                self._to_text(error.stderr)
)
            duration = time.perf_counter() - started_at

            return CommandResult(
                status=CommandStatus.TIMED_OUT,
                command=command,
                working_directory=str(resolved_directory),
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                output_truncated=stdout_truncated or stderr_truncated,
            )

        duration = time.perf_counter() - started_at

        status = (
            CommandStatus.SUCCESS
            if completed.returncode == 0
            else CommandStatus.NON_ZERO_EXIT
        )
        
        stdout, stdout_truncated = self._truncate_output(
            completed.stdout
        )
        stderr, stderr_truncated = self._truncate_output(
            completed.stderr
        )

        return CommandResult(
            status=status,
            command=command,
            working_directory=str(resolved_directory),
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            output_truncated=(
                stdout_truncated or stderr_truncated
            ),
        )
    
    def _truncate_output(
        self,
        output: str,
    ) -> tuple[str, bool]:
        limit = self.max_output_characters

        if len(output) <= limit:
            return output, False

        marker = "\n...<output truncated>...\n"

        if limit <= len(marker):
            return output[:limit], True

        remaining = limit - len(marker)
        head_length = remaining // 2
        tail_length = remaining - head_length

        truncated = (
            output[:head_length]
            + marker
            + output[-tail_length:]
        )

        return truncated, True