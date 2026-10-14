import json

import pytest

from repo_doctor.repair_case import load_repair_case
from repo_doctor.tools.repository import RepositoryError


@pytest.fixture
def case_root(tmp_path):
    production = "src/main/java/Example.java"
    test = "src/test/java/ExampleTest.java"

    for relative in (production, test):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// fixture\n", encoding="utf-8")

    payload = {
        "production_path": production,
        "test_path": test,
        "required_tests": [
            {"class_name": "ExampleTest", "test_name": "boundary"}
        ],
    }
    return tmp_path, payload


def write_case(root, payload):
    (root / "repair-case.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_loads_case(case_root):
    root, payload = case_root
    write_case(root, payload)

    case = load_repair_case(root)

    assert case.production_path == payload["production_path"]
    assert case.test_path == payload["test_path"]
    assert case.required_tests[0].test_name == "boundary"


@pytest.mark.parametrize(
    "production_path",
    [
        "../outside.java",
        "src/test/java/ExampleTest.java",
        "src/main/java/Missing.java",
    ],
)
def test_rejects_invalid_repair_target(case_root, production_path):
    root, payload = case_root
    payload["production_path"] = production_path
    write_case(root, payload)

    with pytest.raises((ValueError, RepositoryError)):
        load_repair_case(root)


def test_rejects_duplicate_required_tests(case_root):
    root, payload = case_root
    payload["required_tests"] *= 2
    write_case(root, payload)

    with pytest.raises(ValueError, match="unique"):
        load_repair_case(root)