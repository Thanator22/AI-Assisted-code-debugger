import pytest

from repo_doctor.repair import RequiredTest, check_required_tests
from repo_doctor.tools.test_reports import (
    TestCaseResult as CaseResult,
    TestCaseStatus as CaseStatus,
    TestReport as Report,
)


REQUIRED = RequiredTest("DiscountTest", "boundary")


def make_report(cases):
    return Report(
        total_tests=len(cases),
        failures=sum(case.status is CaseStatus.FAILED for case in cases),
        errors=sum(case.status is CaseStatus.ERROR for case in cases),
        skipped=sum(case.status is CaseStatus.SKIPPED for case in cases),
        issues=(),
        test_cases=tuple(cases),
    )


def test_accepts_required_test_that_passed():
    report = make_report([
        CaseResult("DiscountTest", "boundary", CaseStatus.PASSED),
    ])

    assert check_required_tests(report, (REQUIRED,)) == ()


@pytest.mark.parametrize(
    "status",
    [CaseStatus.FAILED, CaseStatus.ERROR, CaseStatus.SKIPPED],
)
def test_rejects_required_test_that_did_not_pass(status):
    report = make_report([
        CaseResult("DiscountTest", "boundary", status),
    ])

    diagnostics = check_required_tests(report, (REQUIRED,))

    assert len(diagnostics) == 1
    assert "did not pass" in diagnostics[0]
    assert status.value in diagnostics[0]


def test_other_class_with_same_method_does_not_satisfy_requirement():
    report = make_report([
        CaseResult("OtherTest", "boundary", CaseStatus.PASSED),
    ])

    diagnostics = check_required_tests(report, (REQUIRED,))

    assert "missing" in diagnostics[0]


def test_duplicate_identity_is_ambiguous():
    report = make_report([
        CaseResult("DiscountTest", "boundary", CaseStatus.PASSED),
        CaseResult("DiscountTest", "boundary", CaseStatus.PASSED),
    ])

    diagnostics = check_required_tests(report, (REQUIRED,))

    assert "ambiguous" in diagnostics[0]


def test_rejects_empty_requirements():
    with pytest.raises(ValueError, match="At least one"):
        check_required_tests(make_report([]), ())