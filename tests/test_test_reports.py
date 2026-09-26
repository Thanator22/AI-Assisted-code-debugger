import pytest
from pathlib import Path

from repo_doctor.tools.test_reports import (
    TestReport as Report,
    TestIssueKind as IssueKind,
    ReportCollectionResult,
    ReportCollectionStatus,
    parse_surefire_report,
    collect_surefire_reports,
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
    
def test_available_collection_requires_report():
        with pytest.raises(ValueError, match="requires a report"):
            ReportCollectionResult(
                status=ReportCollectionStatus.AVAILABLE,
                report=None,
        )


@pytest.mark.parametrize(
    "status",
    [
        ReportCollectionStatus.MISSING,
        ReportCollectionStatus.INVALID,
    ],
)
def test_unavailable_collection_rejects_report(status):
    empty_report = Report(
        total_tests=0,
        failures=0,
        errors=0,
        skipped=0,
        issues=(),
    )

    with pytest.raises(ValueError, match="cannot contain a report"):
        ReportCollectionResult(
            status=status,
            report=empty_report,
        )  
def test_combines_reports(tmp_path: Path):
    (tmp_path / "TEST-first.xml").write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase name="a" classname="First"/>'
        '<testcase name="b" classname="First"/>'
        '</testsuite>',
        encoding="utf-8",
    )
    (tmp_path / "TEST-second.xml").write_text(
        '<testsuite tests="1" failures="1" errors="0" skipped="0">'
        '<testcase name="c" classname="Second">'
        '<failure message="Mismatch" type="AssertionError"/>'
        '</testcase>'
        '</testsuite>',
        encoding="utf-8",
    )

    result = collect_surefire_reports(tmp_path)

    assert result.status is ReportCollectionStatus.AVAILABLE
    assert result.report is not None
    assert result.report.total_tests == 3
    assert result.report.passed_tests == 2
    assert result.report.failures == 1
    assert len(result.report.issues) == 1
    assert result.report.issues[0].test_name == "c"


@pytest.mark.parametrize("directory_exists", [True, False])
def test_reports_missing_xml(tmp_path: Path, directory_exists):
    directory = tmp_path if directory_exists else tmp_path / "missing"

    result = collect_surefire_reports(directory)

    assert result.status is ReportCollectionStatus.MISSING
    assert result.report is None


def test_invalid_report_prevents_partial_aggregate(tmp_path: Path):
    (tmp_path / "TEST-valid.xml").write_text(
        '<testsuite tests="0" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    (tmp_path / "TEST-broken.xml").write_text(
        "<testsuite",
        encoding="utf-8",
    )

    result = collect_surefire_reports(tmp_path)

    assert result.status is ReportCollectionStatus.INVALID
    assert result.report is None
    assert any(
        "TEST-broken.xml" in message
        for message in result.diagnostics
    )