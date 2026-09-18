import pytest
from pathlib import Path

from repo_doctor.tools.test_reports import (
    TestReport as Report,
    TestIssueKind as IssueKind,
    parse_surefire_report,
)


@pytest.mark.parametrize(
    "total, failures, errors, skipped, expected_passed, expected_executed",
    [
        (6, 1, 1, 2, 2, 4),  # Mixed outcomes
        (4, 0, 0, 0, 4, 4),  # All passed
        (4, 0, 0, 4, 0, 0),  # All skipped
        (0, 0, 0, 0, 0, 0),  # No tests
    ],
)
def test_calculates_test_counts(
    total,
    failures,
    errors,
    skipped,
    expected_passed,
    expected_executed,
):
    report = Report(
        total_tests=total,
        failures=failures,
        errors=errors,
        skipped=skipped,
        issues=(),
    )

    assert report.passed_tests == expected_passed
    assert report.executed_tests == expected_executed


@pytest.mark.parametrize(
    "total, failures, errors, skipped",
    [
        (-1, 0, 0, 0),
        (4, -1, 0, 0),
        (4, 0, -1, 0),
        (4, 0, 0, -1),
    ],
)
def test_rejects_negative_counts(total, failures, errors, skipped):
    with pytest.raises(ValueError, match="cannot be negative"):
        Report(
            total_tests=total,
            failures=failures,
            errors=errors,
            skipped=skipped,
            issues=(),
        )


def test_rejects_outcomes_exceeding_total():
    with pytest.raises(ValueError, match="exceed total tests"):
        Report(
            total_tests=3,
            failures=2,
            errors=1,
            skipped=1,
            issues=(),
        )
        
def test_parses_failure_and_error_details(tmp_path: Path):
    report_path = tmp_path / "TEST-example.xml"
    report_path.write_text(
        """
        <testsuite tests="3" failures="1" errors="1" skipped="0">
            <testcase name="boundary" classname="DiscountTest">
                <failure
                    message="expected: &lt;900&gt; but was: &lt;1000&gt;"
                    type="AssertionFailedError"
                ><![CDATA[at DiscountTest.boundary(DiscountTest.java:30)]]></failure>
            </testcase>
            <testcase name="unexpectedException" classname="DiscountTest">
                <error message="Unavailable" type="IllegalStateException">
                    error stack trace
                </error>
            </testcase>
            <testcase name="ordinaryPurchase" classname="DiscountTest"/>
        </testsuite>
        """,
        encoding="utf-8",
    )

    report = parse_surefire_report(report_path)

    assert report.total_tests == 3
    assert report.passed_tests == 1
    assert report.executed_tests == 3
    assert len(report.issues) == 2

    failure, error = report.issues

    assert failure.kind is IssueKind.FAILURE
    assert failure.test_name == "boundary"
    assert failure.class_name == "DiscountTest"
    assert failure.message == "expected: <900> but was: <1000>"
    assert failure.exception_type == "AssertionFailedError"
    assert "DiscountTest.java:30" in failure.stack_trace

    assert error.kind is IssueKind.ERROR
    assert error.message == "Unavailable"
    assert error.exception_type == "IllegalStateException"