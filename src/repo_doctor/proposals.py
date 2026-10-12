import json

from repo_doctor.diagnosis import RepairProposal


MAX_RESPONSE_CHARACTERS = 30_000

FIELD_LIMITS = {
    "explanation": 2_000,
    "old_text": 10_000,
    "new_text": 10_000,
}


class ProposalResponseError(ValueError):
    """Raised when a model response violates the proposal contract."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}

    for key, value in pairs:
        if key in result:
            raise ProposalResponseError(
                f"Duplicate JSON field: {key}"
            )
        result[key] = value

    return result


def parse_proposal_response(response: str) -> RepairProposal:
    """Parse one JSON object without repairing malformed model output."""
    if not isinstance(response, str):
        raise ProposalResponseError("Response must be a string")

    if len(response) > MAX_RESPONSE_CHARACTERS:
        raise ProposalResponseError("Response exceeds the size limit")

    try:
        payload = json.loads(
            response,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProposalResponseError(
            "Response must be a single valid JSON object"
        ) from error

    if not isinstance(payload, dict):
        raise ProposalResponseError("Response must be a JSON object")

    if set(payload) != set(FIELD_LIMITS):
        raise ProposalResponseError(
            "Expected exactly: explanation, old_text, new_text"
        )

    for field, limit in FIELD_LIMITS.items():
        value = payload[field]

        if not isinstance(value, str):
            raise ProposalResponseError(
                f"{field} must be a string"
            )

        if len(value) > limit:
            raise ProposalResponseError(
                f"{field} exceeds the size limit"
            )

    if not payload["explanation"].strip():
        raise ProposalResponseError("Explanation cannot be empty")

    if not payload["old_text"].strip():
        raise ProposalResponseError(
            "old_text must identify non-whitespace source text"
        )

    if payload["old_text"] == payload["new_text"]:
        raise ProposalResponseError("Proposal must change the source text")

    return RepairProposal(
        explanation=payload["explanation"],
        old_text=payload["old_text"],
        new_text=payload["new_text"],
    )