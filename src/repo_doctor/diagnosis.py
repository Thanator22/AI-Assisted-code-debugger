from dataclasses import dataclass

from repo_doctor.tools.repository import (
    FileReadResult,
    FileReadStatus,
    Repository,
)
from repo_doctor.tools.test_reports import TestIssue
from repo_doctor.tools.patching import ReplacementRequest


class DiagnosisInputError(Exception):
    """Raised when required diagnosis evidence cannot be read."""


@dataclass(frozen=True)
class FailureEvidence:
    class_name: str
    test_name: str
    message: str
    exception_type: str
    stack_trace: str
    truncated: bool


@dataclass(frozen=True)
class DiagnosisInput:
    failure: FailureEvidence
    bug_report: FileReadResult
    production_code: FileReadResult
    test_code: FileReadResult
    
@dataclass(frozen=True)
class RepairProposal:
    explanation: str
    old_text: str
    new_text: str


class ProposalValidationError(ValueError):
    """Raised when a proposal cannot be tied to inspected code."""


def create_replacement_request(
    diagnosis: DiagnosisInput,
    proposal: RepairProposal,
) -> ReplacementRequest:
    source = diagnosis.production_code

    if (
        source.status is not FileReadStatus.SUCCESS
        or source.file_sha256 is None
    ):
        raise ProposalValidationError(
            "Production code requires a successful read with a hash"
        )

    if not proposal.explanation.strip():
        raise ProposalValidationError(
            "Proposal must explain the change"
        )

    if not proposal.old_text.strip():
        raise ProposalValidationError(
            "Replacement must identify non-whitespace source text"
        )

    if proposal.old_text == proposal.new_text:
        raise ProposalValidationError(
            "Replacement must change the source text"
        )

    first_match = source.content.find(proposal.old_text)

    if first_match == -1:
        raise ProposalValidationError(
            "Replacement text was not present in the inspected excerpt"
        )

    # Starting one character later also detects overlapping matches.
    if source.content.find(proposal.old_text, first_match + 1) != -1:
        raise ProposalValidationError(
            "Replacement text is ambiguous in the inspected excerpt"
        )

    return ReplacementRequest(
        relative_path=source.relative_path,
        expected_sha256=source.file_sha256,
        old_text=proposal.old_text,
        new_text=proposal.new_text,
    )


def build_diagnosis_input(
    repository: Repository,
    issue: TestIssue,
    production_path: str,
    test_path: str,
    *,
    production_start_line: int = 1,
    production_end_line: int = 200,
    test_start_line: int = 1,
    test_end_line: int = 200,
) -> DiagnosisInput:
    """Collect bounded evidence from the repair workspace."""

    def read_required(
        path: str,
        start_line: int,
        end_line: int,
    ) -> FileReadResult:
        result = repository.read_lines(path, start_line, end_line)

        if result.status is not FileReadStatus.SUCCESS:
            raise DiagnosisInputError(
                f"Cannot read {path}: {result.status.value}; "
                f"{result.diagnostic}"
            )

        if result.file_sha256 is None:
            raise DiagnosisInputError(
                f"Read did not provide a file hash: {path}"
            )

        if not result.content.strip():
            raise DiagnosisInputError(
                f"Requested excerpt contains no text: {path}"
            )

        return result

    # Bound report text separately from repository file reads.
    fields = (
        issue.class_name,
        issue.test_name,
        issue.message,
        issue.exception_type,
        issue.stack_trace,
    )
    limits = (500, 500, 2_000, 500, 6_000)
    bounded = tuple(
        value[:limit]
        for value, limit in zip(fields, limits)
    )

    failure = FailureEvidence(
        class_name=bounded[0],
        test_name=bounded[1],
        message=bounded[2],
        exception_type=bounded[3],
        stack_trace=bounded[4],
        truncated=any(
            len(value) > limit
            for value, limit in zip(fields, limits)
        ),
    )

    return DiagnosisInput(
        failure=failure,
        bug_report=read_required("bug-report.md", 1, 200),
        production_code=read_required(
            production_path,
            production_start_line,
            production_end_line,
        ),
        test_code=read_required(
            test_path,
            test_start_line,
            test_end_line,
        ),
    )