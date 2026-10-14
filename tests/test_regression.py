import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from repo_doctor.regression import (
    ASSERTION_MARKER,
    CLASS_NAME,
    METHOD_NAME,
    RegressionError,
    RegressionSpec,
    ModelRegressionGenerator,
    parse_regression_response,
    render_regression,
    create_regression_file,
    validate_red_run,
)
from repo_doctor.tools.repository import Repository


def spec():
    return RegressionSpec(
        "Preserve a fractional result", "average", False, ((1, 2),), 1.5
    )


def test_template_owns_class_method_assertion_and_path(tmp_path):
    generated = render_regression(
        "src/main/java/com/example/AverageService.java", spec()
    )
    assert (
        generated.relative_path
        == "src/test/java/com/example/RepoDoctorGeneratedRegressionTest.java"
    )
    assert generated.class_name == "com.example." + CLASS_NAME
    assert generated.test_name == METHOD_NAME
    assert "new AverageService().average(new int[]{1, 2})" in generated.content
    assert '1e-9, "repo-doctor-regression"' in generated.content
    assert generated.content.count("@org.junit.jupiter.api.Test") == 1
    parent = tmp_path / "src/test/java/com/example"
    parent.mkdir(parents=True)
    create_regression_file(Repository(tmp_path), generated)
    assert (tmp_path / generated.relative_path).read_text() == generated.content
    with pytest.raises(RegressionError, match="FileExistsError"):
        create_regression_file(Repository(tmp_path), generated)
    assert (tmp_path / generated.relative_path).read_text() == generated.content


@pytest.mark.parametrize(
    "change",
    [
        {"method": "average(); System.exit(0)"},
        {"arguments": [True]},
        {"arguments": [[2**31]]},
        {"arguments": ["new int[]{1}"]},
        {"arguments": [[[1]]]},
        {"arguments": [1] * 9},
        {"arguments": [[1] * 101]},
        {"expected": float("nan")},
        {"expected": float("inf")},
        {"expected": 2**54},
        {"expected": True},
        {"static": 1},
        {"explanation": ""},
        {"path": "pom.xml"},
    ],
)
def test_rejects_unsupported_or_code_bearing_payloads(change):
    payload = dict(
        explanation="Fractional average",
        method="average",
        static=False,
        arguments=[[1, 2]],
        expected=1.5,
    )
    payload.update(change)
    with pytest.raises(RegressionError):
        parse_regression_response(json.dumps(payload))


@pytest.mark.parametrize(
    "response",
    [
        '{"abstain":true}',
        '{"method":"a","method":"b"}',
        "```json\n{}\n```",
        "x" * 12001,
    ],
)
def test_invalid_response_is_not_repaired(response):
    with pytest.raises(RegressionError):
        parse_regression_response(response)


def test_forged_content_or_destination_cannot_be_written(tmp_path):
    generated = render_regression("src/main/java/AverageService.java", spec())
    (tmp_path / "src/test/java").mkdir(parents=True)
    for forged in (
        replace(generated, content="bad"),
        replace(generated, relative_path="pom.xml"),
    ):
        with pytest.raises(RegressionError):
            create_regression_file(Repository(tmp_path), forged)
    assert not (tmp_path / "pom.xml").exists()


def test_symlink_target_is_rejected(tmp_path):
    generated = render_regression("src/main/java/AverageService.java", spec())
    parent = tmp_path / "src/test/java"
    parent.mkdir(parents=True)
    other = tmp_path / "other"
    other.write_text("keep")
    try:
        (parent / (CLASS_NAME + ".java")).symlink_to(other)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(RegressionError):
        create_regression_file(Repository(tmp_path), generated)
    assert other.read_text() == "keep"
