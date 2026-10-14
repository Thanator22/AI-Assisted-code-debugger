import json
from dataclasses import dataclass
from pathlib import Path

from repo_doctor.repair import RequiredTest
from repo_doctor.discovery import production_files
from repo_doctor.tools.repository import Repository


@dataclass(frozen=True)
class RepairCase:
    production_path: str | None
    test_path: str | None
    required_tests: tuple[RequiredTest, ...]
    allowed_files: frozenset[str] = frozenset()


def load_repair_case(root: Path) -> RepairCase:
    repository = Repository(root)
    path = repository.resolve_path("repair-case.json")

    with path.open("rb") as source:
        data = source.read(16_385)

    if len(data) > 16_384:
        raise ValueError("Repair case configuration exceeds 16 KiB")

    payload = json.loads(data.decode("utf-8"))

    legacy_fields = {"production_path", "test_path", "required_tests"}
    discovery_fields = {"required_tests"}
    if not isinstance(payload, dict) or set(payload) not in (
        legacy_fields,
        discovery_fields,
    ):
        raise ValueError("Invalid repair case fields")

    def validate_path(field: str, prefix: tuple[str, ...]) -> str:
        value = payload[field]

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a nonempty string")

        resolved = repository.resolve_path(value)
        relative = resolved.relative_to(repository.root)

        if relative.parts[:3] != prefix or resolved.suffix != ".java":
            raise ValueError(f"{field} must identify a Java file under {prefix}")

        if not resolved.is_file():
            raise ValueError(f"{field} must identify an existing file")

        return relative.as_posix()

    if set(payload) == legacy_fields:
        production_path = validate_path("production_path", ("src", "main", "java"))
        test_path = validate_path("test_path", ("src", "test", "java"))
        allowed_files = frozenset({production_path})
    else:
        production_path = None
        test_path = None
        # Application policy for trusted fixtures: existing standard production
        # Java files. Discovery selects within this set; it cannot enlarge it.
        allowed_files = frozenset(production_files(repository))
        if not allowed_files:
            raise ValueError("No production Java files are available for discovery")

    entries = payload["required_tests"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("At least one required test must be specified")

    required_tests = []

    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"class_name", "test_name"}:
            raise ValueError("Invalid required test fields")

        if not all(isinstance(value, str) for value in entry.values()):
            raise ValueError("Required test names must be strings")

        required_tests.append(
            RequiredTest(
                class_name=entry["class_name"],
                test_name=entry["test_name"],
            )
        )

    if len(set(required_tests)) != len(required_tests):
        raise ValueError("Required test identities must be unique")

    return RepairCase(
        production_path=production_path,
        test_path=test_path,
        required_tests=tuple(required_tests),
        allowed_files=allowed_files,
    )
