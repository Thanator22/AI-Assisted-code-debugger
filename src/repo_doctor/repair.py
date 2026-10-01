from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path

from repo_doctor.tools.workspace import RepairWorkspace
from repo_doctor.tools.test_reports import (
    TestCaseStatus,
    TestReport,
    validate_test_case_counts,
)
from repo_doctor.tools.build import (
    MavenTestResult,
    MavenTestRunner,
    TestRunOutcome,
    classify_test_run,
)
from repo_doctor.tools.patching import (
    PatchResult,
    PatchApplier,
    PatchStatus,
    ReplacementRequest,
)



class RepairAttemptStatus(str, Enum):
    BASELINE_REJECTED = "baseline_rejected"
    PATCH_REJECTED = "patch_rejected"
    VERIFICATION_FAILED = "verification_failed"
    INTEGRITY_FAILED = "integrity_failed"
    VERIFIED = "verified"


@dataclass(frozen=True)
class RepairAttemptResult:
    status: RepairAttemptStatus
    workspace: RepairWorkspace
    baseline: MavenTestResult
    patch: PatchResult | None = None
    verification: MavenTestResult | None = None
    diagnostics: tuple[str, ...] = ()
    
@dataclass(frozen=True)
class RequiredTest:
    class_name: str
    test_name: str

    def __post_init__(self) -> None:
        if not self.class_name.strip() or not self.test_name.strip():
            raise ValueError("Required test identity cannot be empty")


def check_required_tests(
    report: TestReport,
    required_tests: tuple[RequiredTest, ...],
) -> tuple[str, ...]:
    if not required_tests:
        raise ValueError("At least one required test must be specified")

    diagnostics: list[str] = []

    for required in required_tests:
        identity = f"{required.class_name}.{required.test_name}"

        matches = [
            case
            for case in report.test_cases
            if case.class_name == required.class_name
            and case.test_name == required.test_name
        ]

        if not matches:
            diagnostics.append(f"Required test is missing: {identity}")
        elif len(matches) > 1:
            diagnostics.append(
                f"Required test identity is ambiguous: {identity}"
            )
        elif matches[0].status is not TestCaseStatus.PASSED:
            diagnostics.append(
                f"Required test did not pass: {identity} "
                f"({matches[0].status.value})"
            )

    return tuple(diagnostics)

def _snapshot_fixture_inputs(root: Path) -> dict[str, str]:
    paths = [root / "pom.xml", root / "bug-report.md"]
    paths.extend(
        path
        for path in (root / "src").rglob("*")
        if path.is_file()
    )

    return {
        path.relative_to(root).as_posix():
            sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    
def run_fixture_repair(
    workspace: RepairWorkspace,
    request: ReplacementRequest,
    allowed_files: set[str],
    required_tests: tuple[RequiredTest, ...],
) -> RepairAttemptResult:
    if not required_tests:
        raise ValueError("At least one required test must be specified")

    # Construct before execution to validate workspace separation.
    applier = PatchApplier(workspace, allowed_files=allowed_files)
    runner = MavenTestRunner(workspace.project_root)

    original_inputs = _snapshot_fixture_inputs(workspace.source_root)
    workspace_inputs = _snapshot_fixture_inputs(workspace.project_root)

    baseline = runner.run_tests()
    patch: PatchResult | None = None
    verification: MavenTestResult | None = None

    def finish(
        status: RepairAttemptStatus,
        diagnostics: tuple[str, ...] = (),
    ) -> RepairAttemptResult:
        expected_inputs = dict(workspace_inputs)

        if patch is not None and patch.status is PatchStatus.APPLIED:
            target = applier.repository.resolve_path(request.relative_path)
            key = target.relative_to(applier.repository.root).as_posix()
            expected_inputs[key] = patch.after_sha256

        integrity_errors: list[str] = []

        try:
            if (
                _snapshot_fixture_inputs(workspace.source_root)
                != original_inputs
            ):
                integrity_errors.append("Original fixture inputs changed")

            if (
                _snapshot_fixture_inputs(workspace.project_root)
                != expected_inputs
            ):
                integrity_errors.append(
                    "Workspace inputs differ from the authorized change"
                )
        except OSError as error:
            integrity_errors.append(
                f"Could not verify input integrity: {type(error).__name__}"
            )

        if integrity_errors:
            status = RepairAttemptStatus.INTEGRITY_FAILED
            diagnostics += tuple(integrity_errors)

        return RepairAttemptResult(
            status=status,
            workspace=workspace,
            baseline=baseline,
            patch=patch,
            verification=verification,
            diagnostics=diagnostics,
        )

    # Gate 1: reproduce the required failures.
    baseline_report = baseline.reports.report

    if (
        classify_test_run(baseline) is not TestRunOutcome.TESTS_FAILED
        or baseline_report is None
    ):
        return finish(
            RepairAttemptStatus.BASELINE_REJECTED,
            ("Baseline did not produce usable failing-test evidence",),
        )

    try:
        validate_test_case_counts(baseline_report)
    except ValueError as error:
        return finish(
            RepairAttemptStatus.BASELINE_REJECTED,
            (str(error),),
        )

    for required in required_tests:
        matches = [
            case
            for case in baseline_report.test_cases
            if case.class_name == required.class_name
            and case.test_name == required.test_name
        ]

        if (
            len(matches) != 1
            or matches[0].status not in (
                TestCaseStatus.FAILED,
                TestCaseStatus.ERROR,
            )
        ):
            return finish(
                RepairAttemptStatus.BASELINE_REJECTED,
                (
                    "Required baseline failure not uniquely reproduced: "
                    f"{required.class_name}.{required.test_name}",
                ),
            )

    # Check that baseline execution itself did not alter fixture inputs.
    baseline_check = finish(RepairAttemptStatus.BASELINE_REJECTED)
    if baseline_check.status is RepairAttemptStatus.INTEGRITY_FAILED:
        return baseline_check

    # Gate 2: apply the caller's proposal without refreshing its hash.
    patch = applier.apply(request)

    if patch.status is not PatchStatus.APPLIED:
        return finish(
            RepairAttemptStatus.PATCH_REJECTED,
            (f"{patch.status.value}: {patch.diagnostic}",),
        )

    # Check the edit before executing the modified project.
    patch_check = finish(RepairAttemptStatus.PATCH_REJECTED)
    if patch_check.status is RepairAttemptStatus.INTEGRITY_FAILED:
        return patch_check

    # Gate 3: execute verification and inspect the required test identities.
    verification = runner.run_tests()
    report = verification.reports.report

    if (
        classify_test_run(verification) is not TestRunOutcome.PASSED
        or report is None
    ):
        return finish(
            RepairAttemptStatus.VERIFICATION_FAILED,
            ("Post-patch test execution did not pass",),
        )

    try:
        validate_test_case_counts(report)
    except ValueError as error:
        return finish(
            RepairAttemptStatus.VERIFICATION_FAILED,
            (str(error),),
        )

    diagnostics = check_required_tests(report, required_tests)

    # Preserve every baseline test identity and its occurrence count.
    from collections import Counter

    baseline_identities = Counter(
        (case.class_name, case.test_name)
        for case in baseline_report.test_cases
    )
    verified_identities = Counter(
        (case.class_name, case.test_name)
        for case in report.test_cases
    )

    if baseline_identities != verified_identities:
        diagnostics += ("Test identities changed between runs",)

    if report.skipped:
        diagnostics += ("Verification contains skipped tests",)

    if diagnostics:
        return finish(
            RepairAttemptStatus.VERIFICATION_FAILED,
            diagnostics,
        )

    return finish(RepairAttemptStatus.VERIFIED)