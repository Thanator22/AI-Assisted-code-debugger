import json
import pytest

from repo_doctor.proposals import (
    MAX_RESPONSE_CHARACTERS,
    ProposalResponseError,
    parse_proposal_response,
)


def valid_payload():
    return {
        "explanation": "Include the threshold.",
        "old_text": "quantity > 10",
        "new_text": "quantity >= 10",
    }


def test_parses_proposal_and_preserves_whitespace():
    payload = valid_payload()
    payload["old_text"] = "    return false;\r\n"
    payload["new_text"] = "    return true;\r\n"

    proposal = parse_proposal_response(json.dumps(payload))

    assert proposal.explanation == payload["explanation"]
    assert proposal.old_text == payload["old_text"]
    assert proposal.new_text == payload["new_text"]


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        "[]",
        "null",
        '{"explanation": "Missing replacement fields"}',
        '```json\n{"explanation": "Example"}\n```',
        (
            '{"explanation":"First","explanation":"Second",'
            '"old_text":"a","new_text":"b"}'
        ),
    ],
)
def test_rejects_invalid_response_structure(response):
    with pytest.raises(ProposalResponseError):
        parse_proposal_response(response)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_path", "pom.xml"),
        ("expected_sha256", "model-supplied-hash"),
        ("explanation", " "),
        ("explanation", "x" * 2_001),
        ("old_text", 123),
        ("old_text", "\n\t"),
        ("old_text", "x" * 10_001),
        ("new_text", None),
        ("new_text", "x" * 10_001),
        ("new_text", "quantity > 10"),
    ],
)
def test_rejects_invalid_fields(field, value):
    payload = valid_payload()
    payload[field] = value

    with pytest.raises(ProposalResponseError):
        parse_proposal_response(json.dumps(payload))


def test_rejects_oversized_response():
    with pytest.raises(ProposalResponseError, match="size limit"):
        parse_proposal_response(
            " " * (MAX_RESPONSE_CHARACTERS + 1)
        )


def test_allows_deletion():
    payload = valid_payload()
    payload["new_text"] = ""

    proposal = parse_proposal_response(json.dumps(payload))

    assert proposal.new_text == ""