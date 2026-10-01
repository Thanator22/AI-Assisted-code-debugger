from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import xml.etree.ElementTree as ET


class TestIssueKind(str, Enum):
    FAILURE = "failure"
    ERROR = "error"


@dataclass(frozen=True)
class TestIssue:
    test_name: str
    class_name: str
    kind: TestIssueKind
    message: str
    exception_type: str
    stack_trace: str

class TestCaseStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TestCaseResult:
    class_name: str
    test_name: str
    status: TestCaseStatus

@dataclass(frozen=True)
class TestReport:
    total_tests: int
    failures: int
    errors: int
    skipped: int
    issues: tuple[TestIssue, ...]
    test_cases: tuple[TestCaseResult, ...]

    def __post_init__(self) -> None:
        counts = (
            self.total_tests,
            self.failures,
            self.errors,
            self.skipped,
        )

        if any(count < 0 for count in counts):
            raise ValueError("Test counts cannot be negative")

        if self.failures + self.errors + self.skipped > self.total_tests:
            raise ValueError(
                "Failures, errors, and skipped tests exceed total tests"
            )

    @property
    def passed_tests(self) -> int:
        return (
            self.total_tests
            - self.failures
            - self.errors
            - self.skipped
        )

    @property
    def executed_tests(self) -> int:
        return self.total_tests - self.skipped
    
def parse_surefire_report(report_path: Path) -> TestReport:
    root = ET.parse(report_path).getroot()

    if root.tag != "testsuite":
        raise ValueError("Expected a testsuite XML root")

    def read_count(attribute: str) -> int:
        value = root.get(attribute)

        if value is None:
            raise ValueError(
                f"Missing test count attribute: {attribute}"
            )

        return int(value)

    issues: list[TestIssue] = []
    test_cases: list[TestCaseResult] = []

    for testcase in root.findall("testcase"):
        class_name = testcase.attrib["classname"]
        test_name = testcase.attrib["name"]

        has_failure = testcase.find("failure") is not None
        has_error = testcase.find("error") is not None
        has_skipped = testcase.find("skipped") is not None

        if sum((has_failure, has_error, has_skipped)) > 1:
            raise ValueError(
                f"Conflicting test outcomes: {class_name}.{test_name}"
            )

        if has_failure:
            case_status = TestCaseStatus.FAILED
        elif has_error:
            case_status = TestCaseStatus.ERROR
        elif has_skipped:
            case_status = TestCaseStatus.SKIPPED
        else:
            case_status = TestCaseStatus.PASSED

        # Record every testcase, including those with no child elements.
        test_cases.append(
            TestCaseResult(
                class_name=class_name,
                test_name=test_name,
                status=case_status,
            )
        )

        # Separately collect details for failures and errors.
        for child in testcase:
            if child.tag == "failure":
                kind = TestIssueKind.FAILURE
            elif child.tag == "error":
                kind = TestIssueKind.ERROR
            else:
                continue

            issues.append(
                TestIssue(
                    test_name=test_name,
                    class_name=class_name,
                    kind=kind,
                    message=child.get("message", ""),
                    exception_type=child.get("type", ""),
                    stack_trace="".join(child.itertext()).strip(),
                )
            )

    report = TestReport(
    total_tests=read_count("tests"),
    failures=read_count("failures"),
    errors=read_count("errors"),
    skipped=read_count("skipped"),
    issues=tuple(issues),
    test_cases=tuple(test_cases),
)

    validate_test_case_counts(report)
    return report
    
    
class ReportCollectionStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class ReportCollectionResult:
    status: ReportCollectionStatus
    report: TestReport | None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is ReportCollectionStatus.AVAILABLE:
            if self.report is None:
                raise ValueError(
                    "Available report collection requires a report"
                )
        elif self.report is not None:
            raise ValueError(
                "Missing or invalid report collection cannot contain a report"
            )
def collect_surefire_reports(
    report_directory: Path,
) -> ReportCollectionResult:
    try:
        entries = list(report_directory.iterdir())
    except FileNotFoundError:
        return ReportCollectionResult(
            status=ReportCollectionStatus.MISSING,
            report=None,
            diagnostics=("Report directory does not exist.",),
        )
    except OSError as error:
        return ReportCollectionResult(
            status=ReportCollectionStatus.INVALID,
            report=None,
            diagnostics=(f"Cannot read report directory: {error}",),
        )

    report_paths = sorted(
        path
        for path in entries
        if path.name.startswith("TEST-")
        and path.name.endswith(".xml")
    )

    if not report_paths:
        return ReportCollectionResult(
            status=ReportCollectionStatus.MISSING,
            report=None,
            diagnostics=("No test report XML files were found.",),
        )

    reports: list[TestReport] = []
    diagnostics: list[str] = []

    for path in report_paths:
        try:
            reports.append(parse_surefire_report(path))
        except (OSError, ET.ParseError, ValueError, KeyError) as error:
            diagnostics.append(
                f"{path.name}: {type(error).__name__}: {error}"
            )

    if diagnostics:
        return ReportCollectionResult(
            status=ReportCollectionStatus.INVALID,
            report=None,
            diagnostics=tuple(diagnostics),
        )

    aggregate = TestReport(
        total_tests=sum(report.total_tests for report in reports),
        failures=sum(report.failures for report in reports),
        errors=sum(report.errors for report in reports),
        skipped=sum(report.skipped for report in reports),
        issues=tuple(
            issue
            for report in reports
            for issue in report.issues
        ),
        test_cases=tuple(
            case
            for report in reports
            for case in report.test_cases
        ),
    )

    return ReportCollectionResult(
        status=ReportCollectionStatus.AVAILABLE,
        report=aggregate,
    )
    
def validate_test_case_counts(report: TestReport) -> None:
    observed = (
        len(report.test_cases),
        sum(
            case.status is TestCaseStatus.FAILED
            for case in report.test_cases
        ),
        sum(
            case.status is TestCaseStatus.ERROR
            for case in report.test_cases
        ),
        sum(
            case.status is TestCaseStatus.SKIPPED
            for case in report.test_cases
        ),
    )

    declared = (
        report.total_tests,
        report.failures,
        report.errors,
        report.skipped,
    )

    if observed != declared:
        raise ValueError(
            "Testcase outcomes disagree with suite counts: "
            f"declared={declared}, observed={observed} "
            "(total, failures, errors, skipped)"
        )