from hashlib import sha256

import pytest

from repo_doctor.diagnosis import (
    DiagnosisInputError,
    RepairProposal,
    ProposalValidationError,
    RepairProposal,
    build_diagnosis_input,
    create_replacement_request,
)
from repo_doctor.tools.repository import Repository
from repo_doctor.tools.test_reports import TestIssue as Issue, TestIssueKind as IssueKind


def make_issue(message: str = "expected 900 but was 1000") -> Issue:
    return Issue(
        test_name="boundary",
        class_name="ExampleTest",
        kind=IssueKind.FAILURE,
        message=message,
        exception_type="AssertionError",
        stack_trace="at ExampleTest.boundary(ExampleTest.java:10)",
    )


def prepare_repository(tmp_path) -> Repository:
    (tmp_path / "bug-report.md").write_text(
        "Apply the discount at the threshold.",
        encoding="utf-8",
    )
    (tmp_path / "Example.java").write_bytes(
        b"class Example {\r\n"
        b"    // production code\r\n"
        b"}\r\n"
    )
    (tmp_path / "ExampleTest.java").write_text(
        "class ExampleTest {}\n",
        encoding="utf-8",
    )
    return Repository(tmp_path)


def test_collects_evidence_with_full_file_hash(tmp_path):
    repository = prepare_repository(tmp_path)

    result = build_diagnosis_input(
        repository,
        make_issue(),
        "Example.java",
        "ExampleTest.java",
        production_start_line=2,
        production_end_line=2,
    )

    assert result.failure.test_name == "boundary"
    assert result.production_code.content == (
        "    // production code\r\n"
    )
    assert result.production_code.file_sha256 == sha256(
        (tmp_path / "Example.java").read_bytes()
    ).hexdigest()
    assert result.test_code.content == "class ExampleTest {}\r\n"


def test_rejects_missing_required_evidence(tmp_path):
    repository = prepare_repository(tmp_path)
    (tmp_path / "ExampleTest.java").unlink()

    with pytest.raises(DiagnosisInputError, match="not_found"):
        build_diagnosis_input(
            repository,
            make_issue(),
            "Example.java",
            "ExampleTest.java",
        )


def test_bounds_failure_message_and_marks_truncation(tmp_path):
    repository = prepare_repository(tmp_path)

    result = build_diagnosis_input(
        repository,
        make_issue(message="x" * 2_001),
        "Example.java",
        "ExampleTest.java",
    )

    assert result.failure.message == "x" * 2_000
    assert result.failure.truncated is True
    
def test_binds_proposal_to_inspected_file_version(tmp_path):
    repository = prepare_repository(tmp_path)
    diagnosis = build_diagnosis_input(
        repository,
        make_issue(),
        "Example.java",
        "ExampleTest.java",
    )
    captured_hash = diagnosis.production_code.file_sha256

    # A later file change must not silently refresh the expected hash.
    (tmp_path / "Example.java").write_text(
        "class Changed {}\n",
        encoding="utf-8",
    )

    request = create_replacement_request(
        diagnosis,
        RepairProposal(
            explanation="Update the production comment.",
            old_text="// production code",
            new_text="// updated production code",
        ),
    )

    assert request.relative_path == "Example.java"
    assert request.expected_sha256 == captured_hash
    assert request.expected_sha256 != sha256(
        (tmp_path / "Example.java").read_bytes()
    ).hexdigest()


def test_rejects_edit_outside_inspected_excerpt(tmp_path):
    repository = prepare_repository(tmp_path)
    diagnosis = build_diagnosis_input(
        repository,
        make_issue(),
        "Example.java",
        "ExampleTest.java",
        production_start_line=2,
        production_end_line=2,
    )

    with pytest.raises(
        ProposalValidationError,
        match="not present",
    ):
        create_replacement_request(
            diagnosis,
            RepairProposal(
                explanation="Rename the class.",
                old_text="class Example",
                new_text="class Renamed",
            ),
        )


def test_rejects_ambiguous_edit_in_excerpt(tmp_path):
    repository = prepare_repository(tmp_path)
    (tmp_path / "Example.java").write_text(
        "class Example { int a = 10; int b = 10; }\n",
        encoding="utf-8",
    )
    diagnosis = build_diagnosis_input(
        repository,
        make_issue(),
        "Example.java",
        "ExampleTest.java",
    )

    with pytest.raises(
        ProposalValidationError,
        match="ambiguous",
    ):
        create_replacement_request(
            diagnosis,
            RepairProposal(
                explanation="Change one threshold.",
                old_text="10",
                new_text="11",
            ),
        )