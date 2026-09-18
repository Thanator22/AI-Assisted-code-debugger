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


@dataclass(frozen=True)
class TestReport:
    total_tests: int
    failures: int
    errors: int
    skipped: int
    issues: tuple[TestIssue, ...]

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

    for testcase in root.findall("testcase"):
        for child in testcase:
            if child.tag == "failure":
                kind = TestIssueKind.FAILURE
            elif child.tag == "error":
                kind = TestIssueKind.ERROR
            else:
                continue

            issues.append(
                TestIssue(
                    test_name=testcase.attrib["name"],
                    class_name=testcase.attrib["classname"],
                    kind=kind,
                    message=child.get("message", ""),
                    exception_type=child.get("type", ""),
                    stack_trace="".join(child.itertext()).strip(),
                )
            )

    return TestReport(
        total_tests=read_count("tests"),
        failures=read_count("failures"),
        errors=read_count("errors"),
        skipped=read_count("skipped"),
        issues=tuple(issues),
    )