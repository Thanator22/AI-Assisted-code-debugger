from collections import Counter
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from enum import Enum
from hashlib import sha256
from pathlib import Path
from collections.abc import Callable

from repo_doctor.diagnosis import (
    DiagnosisInput,
    DiagnosisInputError,
    ProposalValidationError,
    RepairProposal,
    build_diagnosis_input,
    build_discovered_diagnosis_input,
    create_replacement_request,
)

from repo_doctor.regression import (
    RegressionError,
    RegressionSpec,
    GeneratedRegression,
    render_regression,
    create_regression_file,
    validate_red_run,
)
from repo_doctor.tools.workspace import RepairWorkspace
from repo_doctor.tools.test_reports import (
    ReportCollectionStatus,
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
    REGRESSION_REJECTED = "regression_rejected"
    BASELINE_REJECTED = "baseline_rejected"
    PATCH_REJECTED = "patch_rejected"
    VERIFICATION_FAILED = "verification_failed"
    INTEGRITY_FAILED = "integrity_failed"
    VERIFIED = "verified"
    DIAGNOSIS_FAILED = "diagnosis_failed"
    PROPOSAL_FAILED = "proposal_failed"


@dataclass(frozen=True)
class RepairAttemptRecord:
    number: int
    status: RepairAttemptStatus
    proposal: RepairProposal | None = None
    request: ReplacementRequest | None = None
    patch: PatchResult | None = None
    verification: MavenTestResult | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number < 1:
            raise ValueError("Attempt number must be a positive integer")


@dataclass(frozen=True)
class RepairAttemptResult:
    status: RepairAttemptStatus
    workspace: RepairWorkspace
    baseline: MavenTestResult
    patch: PatchResult | None = None
    verification: MavenTestResult | None = None
    diagnostics: tuple[str, ...] = ()
    proposal: RepairProposal | None = None
    request: ReplacementRequest | None = None
    attempts: tuple[RepairAttemptRecord, ...] = ()
    regression: GeneratedRegression | None = None
    regression_run: MavenTestResult | None = None
    regression_accepted: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")

    def allows_retry(
        self,
        *,
        attempts_completed: int,
        verification_outcome: TestRunOutcome,
    ) -> bool:
        if type(attempts_completed) is not int or attempts_completed < 1:
            raise ValueError("attempts_completed must be a positive integer")

        return (
            attempts_completed < self.max_attempts
            and verification_outcome is TestRunOutcome.TESTS_FAILED
        )


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
            diagnostics.append(f"Required test identity is ambiguous: {identity}")
        elif matches[0].status is not TestCaseStatus.PASSED:
            diagnostics.append(
                f"Required test did not pass: {identity} "
                f"({matches[0].status.value})"
            )

    return tuple(diagnostics)


def _snapshot_fixture_inputs(root: Path) -> dict[str, str]:
    paths = [root / "pom.xml", root / "bug-report.md"]
    paths.extend(path for path in (root / "src").rglob("*") if path.is_file())

    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


class RepairState(TypedDict, total=False):
    """Per-run evidence and results; credentials and clients stay outside state."""

    baseline: MavenTestResult
    diagnosis: DiagnosisInput
    proposal: RepairProposal | None
    request: ReplacementRequest | None
    patch: PatchResult | None
    verification: MavenTestResult | None
    status: RepairAttemptStatus | None
    diagnostics: tuple[str, ...]
    result: RepairAttemptResult
    evidence_run: MavenTestResult
    attempt_number: int
    attempts: tuple[RepairAttemptRecord, ...]
    expected_inputs: dict[str, str]
    regression: GeneratedRegression | None
    regression_run: MavenTestResult | None
    regression_accepted: bool
    retry_eligible: bool


def run_fixture_repair(
    workspace: RepairWorkspace,
    request: ReplacementRequest | None,
    allowed_files: set[str],
    required_tests: tuple[RequiredTest, ...],
    *,
    propose_fix: Callable[[DiagnosisInput], RepairProposal] | None = None,
    production_path: str | None = None,
    test_path: str | None = None,
    retry_policy: RetryPolicy | None = None,
    generate_regression: Callable[[DiagnosisInput], RegressionSpec] | None = None,
) -> RepairAttemptResult:
    policy = retry_policy if retry_policy is not None else RetryPolicy()
    if not required_tests:
        raise ValueError("At least one required test must be specified")
    if (request is None) == (propose_fix is None):
        raise ValueError("Provide exactly one of request or propose_fix")

    if generate_regression is not None and propose_fix is None:
        raise ValueError("Regression generation requires a model proposer")

    if propose_fix is not None:
        if (production_path is None) != (test_path is None):
            raise ValueError(
                "Provide both production_path and test_path, or neither for discovery"
            )

    # Construct before execution to validate workspace separation.
    applier = PatchApplier(workspace, allowed_files=allowed_files)
    if propose_fix is not None and production_path is not None:
        approved_paths = {
            applier.repository.resolve_path(path) for path in allowed_files
        }
        target = applier.repository.resolve_path(production_path)

        if target not in approved_paths:
            raise ValueError("Diagnosis production file must be approved for repair")

    runner = MavenTestRunner(workspace.project_root)
    original_inputs = _snapshot_fixture_inputs(workspace.source_root)
    workspace_inputs = _snapshot_fixture_inputs(workspace.project_root)

    def checked_update(
        state: RepairState,
        status: RepairAttemptStatus | None,
        diagnostics: tuple[str, ...] = (),
    ) -> RepairState:
        expected_inputs = state["expected_inputs"]

        integrity_errors: list[str] = []

        try:
            if _snapshot_fixture_inputs(workspace.source_root) != original_inputs:
                integrity_errors.append("Original fixture inputs changed")

            if _snapshot_fixture_inputs(workspace.project_root) != expected_inputs:
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

        return {"status": status, "diagnostics": diagnostics}

    def baseline_node(state: RepairState) -> RepairState:
        baseline = runner.run_tests()
        current = {**state, "baseline": baseline}
        return {
            "baseline": baseline,
            "evidence_run": baseline,
            **validate_baseline(current),
        }

    def validate_baseline(state: RepairState) -> RepairState:
        baseline = state["baseline"]
        # Gate 1: reproduce the required failures.
        baseline_report = baseline.reports.report

        if (
            classify_test_run(baseline) is not TestRunOutcome.TESTS_FAILED
            or baseline_report is None
            or baseline.reports.status is not ReportCollectionStatus.AVAILABLE
        ):
            return checked_update(
                state,
                RepairAttemptStatus.BASELINE_REJECTED,
                ("Baseline did not produce usable failing-test evidence",),
            )

        try:
            validate_test_case_counts(baseline_report)
        except ValueError as error:
            return checked_update(
                state,
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

            if len(matches) != 1 or matches[0].status not in (
                TestCaseStatus.FAILED,
                TestCaseStatus.ERROR,
            ):
                return checked_update(
                    state,
                    RepairAttemptStatus.BASELINE_REJECTED,
                    (
                        "Required baseline failure not uniquely reproduced: "
                        f"{required.class_name}.{required.test_name}",
                    ),
                )

        return checked_update(state, None)

    def begin_attempt_node(state: RepairState) -> RepairState:
        # Previous attempt data is retained in history, never reused as fresh evidence.
        return {
            "attempt_number": state["attempt_number"] + 1,
            "proposal": None,
            "request": request if propose_fix is None else None,
            "patch": None,
            "verification": None,
            "retry_eligible": False,
            **checked_update(state, None),
        }

    def diagnosis_node(state: RepairState) -> RepairState:
        report = state["evidence_run"].reports.report
        assert report is not None
        failed_cases = [
            case
            for case in report.test_cases
            if case.status in (TestCaseStatus.FAILED, TestCaseStatus.ERROR)
        ]
        # Prefer a remaining required failure; otherwise focus on a regression.
        required_order = {
            (test.class_name, test.test_name): index
            for index, test in enumerate(required_tests)
        }
        failed_cases.sort(
            key=lambda case: required_order.get(
                (case.class_name, case.test_name), len(required_order)
            )
        )
        if not failed_cases:
            return checked_update(
                state,
                RepairAttemptStatus.DIAGNOSIS_FAILED,
                ("Diagnosis requires a current failing test",),
            )
        focus = failed_cases[0]
        matching_cases = [
            case
            for case in report.test_cases
            if (case.class_name, case.test_name) == (focus.class_name, focus.test_name)
        ]
        matching_issues = [
            issue
            for issue in report.issues
            if (issue.class_name, issue.test_name)
            == (focus.class_name, focus.test_name)
        ]
        if len(matching_cases) != 1 or len(matching_issues) != 1:
            return checked_update(
                state,
                RepairAttemptStatus.DIAGNOSIS_FAILED,
                ("Diagnosis requires one matching failure detail",),
            )

        try:
            if production_path is None and test_path is None:
                diagnosis = build_discovered_diagnosis_input(
                    applier.repository, matching_issues[0], allowed_files
                )
            else:
                assert production_path is not None and test_path is not None
                diagnosis = build_diagnosis_input(
                    applier.repository, matching_issues[0], production_path, test_path
                )
        except DiagnosisInputError as error:
            return checked_update(
                state,
                RepairAttemptStatus.DIAGNOSIS_FAILED,
                (str(error),),
            )

        return {"diagnosis": diagnosis, **checked_update(state, None)}

    def regression_node(state: RepairState) -> RepairState:
        if generate_regression is None or state.get("regression_accepted"):
            return checked_update(state, None)
        check = checked_update(state, None)
        if check["status"] is not None:
            return check
        try:
            spec = generate_regression(state["diagnosis"])
            check = checked_update(state, None)
            if check["status"] is not None:
                return check
            generated = render_regression(
                state["diagnosis"].production_code.relative_path, spec
            )
            # Production patches never gain permission to edit this test.
            create_regression_file(applier.repository, generated)
        except Exception as error:
            return checked_update(
                state,
                RepairAttemptStatus.REGRESSION_REJECTED,
                (
                    "Regression generation/create failed: "
                    + (
                        str(error)
                        if isinstance(error, RegressionError)
                        else type(error).__name__
                    ),
                ),
            )
        expected = dict(state["expected_inputs"])
        expected[generated.relative_path] = generated.file_sha256
        current = {**state, "expected_inputs": expected, "regression": generated}
        update = {"expected_inputs": expected, "regression": generated}
        check = checked_update(current, None)
        return {**update, **check}

    def reproduce_regression_node(state: RepairState) -> RepairState:
        if generate_regression is None or state.get("regression_accepted"):
            return checked_update(state, None)
        check = checked_update(state, None)
        if check["status"] is not None:
            return check
        generated = state["regression"]
        assert generated is not None
        red = runner.run_tests()
        try:
            validate_red_run(state["baseline"], red, generated)
        except ValueError as error:
            return {
                "regression_run": red,
                **checked_update(
                    state, RepairAttemptStatus.REGRESSION_REJECTED, (str(error),)
                ),
            }
        check = checked_update(state, None)
        return {
            "regression_run": red,
            "regression_accepted": check["status"] is None,
            **check,
        }

    def proposal_node(state: RepairState) -> RepairState:
        assert propose_fix is not None
        diagnosis = state["diagnosis"]
        try:
            proposal = propose_fix(diagnosis)
        except Exception as error:
            # A failed proposer must still pass through integrity checking.
            return checked_update(
                state,
                RepairAttemptStatus.PROPOSAL_FAILED,
                (f"Proposal generation failed: {type(error).__name__}",),
            )

        state = {
            **state,
            "proposal": (proposal if isinstance(proposal, RepairProposal) else None),
        }
        check = checked_update(state, None)
        if check["status"] is RepairAttemptStatus.INTEGRITY_FAILED:
            return {"proposal": state["proposal"], **check}

        if not isinstance(proposal, RepairProposal):
            return checked_update(
                state,
                RepairAttemptStatus.PROPOSAL_FAILED,
                ("Proposer did not return a RepairProposal",),
            )

        try:
            request = create_replacement_request(diagnosis, proposal)
        except ProposalValidationError as error:
            return {
                "proposal": proposal,
                **checked_update(
                    state, RepairAttemptStatus.PROPOSAL_FAILED, (str(error),)
                ),
            }

        return {"proposal": proposal, "request": request}

    def patch_node(state: RepairState) -> RepairState:
        check = checked_update(state, None)
        if check["status"] is not None:
            return check
        current_request = state.get("request")
        assert current_request is not None
        patch = applier.apply(current_request)
        if patch.status is not PatchStatus.APPLIED:
            return {
                "patch": patch,
                **checked_update(
                    state,
                    RepairAttemptStatus.PATCH_REJECTED,
                    (f"{patch.status.value}: {patch.diagnostic}",),
                ),
            }
        target = applier.repository.resolve_path(current_request.relative_path)
        key = target.relative_to(applier.repository.root).as_posix()
        expected = dict(state["expected_inputs"])
        if patch.before_sha256 != expected.get(key) or not patch.after_sha256:
            return {
                "patch": patch,
                **checked_update(
                    state,
                    RepairAttemptStatus.INTEGRITY_FAILED,
                    ("Applied patch does not extend the authorized hash chain",),
                ),
            }
        expected[key] = patch.after_sha256
        current = {**state, "expected_inputs": expected}
        return {
            "patch": patch,
            "expected_inputs": expected,
            **checked_update(current, None),
        }

    def verification_node(state: RepairState) -> RepairState:
        verification = runner.run_tests()
        current = {**state, "verification": verification}
        return {"verification": verification, **validate_verification(current)}

    def validate_verification(state: RepairState) -> RepairState:
        baseline_report = state["baseline"].reports.report
        assert baseline_report is not None
        verification = state["verification"]
        assert verification is not None
        outcome = classify_test_run(verification)
        report = verification.reports.report

        if (
            outcome not in (TestRunOutcome.PASSED, TestRunOutcome.TESTS_FAILED)
            or report is None
            or verification.reports.status is not ReportCollectionStatus.AVAILABLE
        ):
            return checked_update(
                state,
                RepairAttemptStatus.VERIFICATION_FAILED,
                ("Post-patch test execution did not produce usable results",),
            )
        try:
            validate_test_case_counts(report)
        except ValueError as error:
            return checked_update(
                state, RepairAttemptStatus.VERIFICATION_FAILED, (str(error),)
            )

        baseline_identities = Counter(
            (case.class_name, case.test_name) for case in baseline_report.test_cases
        )
        generated = state.get("regression")
        if state.get("regression_accepted") and generated is not None:
            baseline_identities[(generated.class_name, generated.test_name)] += 1
        verified_identities = Counter(
            (case.class_name, case.test_name) for case in report.test_cases
        )
        diagnostics: tuple[str, ...] = ()
        if baseline_identities != verified_identities:
            diagnostics += ("Test identities changed between runs",)
        if report.skipped:
            diagnostics += ("Verification contains skipped tests",)
        if diagnostics:
            return checked_update(
                state, RepairAttemptStatus.VERIFICATION_FAILED, diagnostics
            )

        if outcome is TestRunOutcome.TESTS_FAILED:
            check = checked_update(
                state,
                RepairAttemptStatus.VERIFICATION_FAILED,
                ("Post-patch tests still fail",),
            )
            return {
                "retry_eligible": check["status"]
                is RepairAttemptStatus.VERIFICATION_FAILED,
                "evidence_run": verification,
                **check,
            }

        diagnostics = check_required_tests(report, required_tests)
        return checked_update(
            state,
            (
                RepairAttemptStatus.VERIFICATION_FAILED
                if diagnostics
                else RepairAttemptStatus.VERIFIED
            ),
            diagnostics,
        )

    def record_attempt_node(state: RepairState) -> RepairState:
        status = state.get("status")
        assert status is not None
        # Check once more before the retry decision or final result.
        check = checked_update(state, status, state.get("diagnostics", ()))
        status = check["status"]
        assert status is not None
        record = RepairAttemptRecord(
            number=state["attempt_number"],
            status=status,
            proposal=state.get("proposal"),
            request=state.get("request"),
            patch=state.get("patch"),
            verification=state.get("verification"),
            diagnostics=check["diagnostics"],
        )
        return {"attempts": (*state["attempts"], record), **check}

    def finalize_node(state: RepairState) -> RepairState:
        status = state.get("status")
        assert status is not None
        return {
            "result": RepairAttemptResult(
                status=status,
                workspace=workspace,
                baseline=state["baseline"],
                patch=state.get("patch"),
                verification=state.get("verification"),
                diagnostics=state.get("diagnostics", ()),
                proposal=state.get("proposal"),
                request=state.get("request"),
                attempts=state["attempts"],
                regression=state.get("regression"),
                regression_run=state.get("regression_run"),
                regression_accepted=state.get("regression_accepted", False),
            )
        }

    def route_after_baseline(state: RepairState) -> str:
        return "finalize" if state.get("status") is not None else "begin_attempt"

    def route_after_begin(state: RepairState) -> str:
        if state.get("status") is not None:
            return "record_attempt"
        return "collect_diagnosis" if propose_fix is not None else "apply_patch"

    def continue_or_record(next_node: str):
        def route(state: RepairState) -> str:
            return "record_attempt" if state.get("status") is not None else next_node

        return route

    def route_after_attempt(state: RepairState) -> str:
        verification = state.get("verification")
        if (
            propose_fix is not None
            and state["status"] is RepairAttemptStatus.VERIFICATION_FAILED
            and state["retry_eligible"]
            and verification is not None
            and policy.allows_retry(
                attempts_completed=state["attempt_number"],
                verification_outcome=classify_test_run(verification),
            )
        ):
            return "begin_attempt"
        return "finalize"

    builder = StateGraph(RepairState)
    builder.add_node("run_baseline", baseline_node)
    builder.add_node("begin_attempt", begin_attempt_node)
    builder.add_node("collect_diagnosis", diagnosis_node)
    builder.add_node("generate_regression", regression_node)
    builder.add_node("reproduce_regression", reproduce_regression_node)
    builder.add_node("generate_proposal", proposal_node)
    builder.add_node("apply_patch", patch_node)
    builder.add_node("verify", verification_node)
    builder.add_node("record_attempt", record_attempt_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "run_baseline")
    builder.add_conditional_edges(
        "run_baseline",
        route_after_baseline,
        {name: name for name in ("begin_attempt", "finalize")},
    )
    builder.add_conditional_edges(
        "begin_attempt",
        route_after_begin,
        {name: name for name in ("collect_diagnosis", "apply_patch", "record_attempt")},
    )
    for node, successor in (
        ("collect_diagnosis", "generate_regression"),
        ("generate_regression", "reproduce_regression"),
        ("reproduce_regression", "generate_proposal"),
        ("generate_proposal", "apply_patch"),
        ("apply_patch", "verify"),
    ):
        builder.add_conditional_edges(
            node,
            continue_or_record(successor),
            {successor: successor, "record_attempt": "record_attempt"},
        )
    builder.add_edge("verify", "record_attempt")
    builder.add_conditional_edges(
        "record_attempt",
        route_after_attempt,
        {name: name for name in ("begin_attempt", "finalize")},
    )
    builder.add_edge("finalize", END)

    graph = builder.compile()
    final_state = graph.invoke(
        {
            "request": request,
            "proposal": None,
            "patch": None,
            "verification": None,
            "status": None,
            "diagnostics": (),
            "attempt_number": 0,
            "attempts": (),
            "expected_inputs": workspace_inputs,
            "retry_eligible": False,
            "regression": None,
            "regression_run": None,
            "regression_accepted": False,
        },
        # Budget accounts for all nodes, not just model calls.
        config={"recursion_limit": 10 * policy.max_attempts + 10},
    )
    return final_state["result"]
