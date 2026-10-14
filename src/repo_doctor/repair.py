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
    create_replacement_request,
)

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
            raise ValueError(
                "Attempt number must be a positive integer"
            )


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
    
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.max_attempts) is not int
            or self.max_attempts < 1
        ):
            raise ValueError(
                "max_attempts must be a positive integer"
            )

    def allows_retry(
        self,
        *,
        attempts_completed: int,
        verification_outcome: TestRunOutcome,
    ) -> bool:
        if (
            type(attempts_completed) is not int
            or attempts_completed < 1
        ):
            raise ValueError(
                "attempts_completed must be a positive integer"
            )

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


def run_fixture_repair(
    workspace: RepairWorkspace,
    request: ReplacementRequest | None,
    allowed_files: set[str],
    required_tests: tuple[RequiredTest, ...],
    *,
    propose_fix: Callable[[DiagnosisInput], RepairProposal] | None = None,
    production_path: str | None = None,
    test_path: str | None = None,
) -> RepairAttemptResult:
    if not required_tests:
        raise ValueError("At least one required test must be specified")
    if (request is None) == (propose_fix is None):
        raise ValueError("Provide exactly one of request or propose_fix")

    if propose_fix is not None:
        if not production_path or not test_path:
            raise ValueError(
                "Proposal generation requires production_path and test_path"
            )

    # Construct before execution to validate workspace separation.
    applier = PatchApplier(workspace, allowed_files=allowed_files)
    if propose_fix is not None:
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
        patch = state.get("patch")
        request = state.get("request")
        expected_inputs = dict(workspace_inputs)

        if patch is not None and patch.status is PatchStatus.APPLIED:
            assert request is not None
            target = applier.repository.resolve_path(request.relative_path)
            key = target.relative_to(applier.repository.root).as_posix()
            expected_inputs[key] = patch.after_sha256

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
        return {"baseline": baseline, **validate_baseline(current)}

    def validate_baseline(state: RepairState) -> RepairState:
        baseline = state["baseline"]
        # Gate 1: reproduce the required failures.
        baseline_report = baseline.reports.report

        if (
            classify_test_run(baseline) is not TestRunOutcome.TESTS_FAILED
            or baseline_report is None
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

    def diagnosis_node(state: RepairState) -> RepairState:
        baseline_report = state["baseline"].reports.report
        assert baseline_report is not None
        # Use the first required failure as the diagnosis focus.
        # All required tests still participate in verification.
        focus = required_tests[0]
        matching_issues = [
            issue
            for issue in baseline_report.issues
            if issue.class_name == focus.class_name
            and issue.test_name == focus.test_name
        ]

        if len(matching_issues) != 1:
            return checked_update(
                state,
                RepairAttemptStatus.DIAGNOSIS_FAILED,
                ("Diagnosis requires one matching failure detail",),
            )

        assert production_path is not None
        assert test_path is not None

        try:
            diagnosis = build_diagnosis_input(
                applier.repository,
                matching_issues[0],
                production_path,
                test_path,
            )
        except DiagnosisInputError as error:
            return checked_update(
                state,
                RepairAttemptStatus.DIAGNOSIS_FAILED,
                (str(error),),
            )

        return {"diagnosis": diagnosis, **checked_update(state, None)}

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
        request = state.get("request")
        assert request is not None
        patch = applier.apply(request)
        current = {**state, "patch": patch}
        if patch.status is not PatchStatus.APPLIED:
            return {
                "patch": patch,
                **checked_update(
                    current,
                    RepairAttemptStatus.PATCH_REJECTED,
                    (f"{patch.status.value}: {patch.diagnostic}",),
                ),
            }
        return {"patch": patch, **checked_update(current, None)}

    def verification_node(state: RepairState) -> RepairState:
        verification = runner.run_tests()
        current = {**state, "verification": verification}
        return {"verification": verification, **validate_verification(current)}

    def validate_verification(state: RepairState) -> RepairState:
        baseline_report = state["baseline"].reports.report
        assert baseline_report is not None
        # Gate 3: execute verification and inspect the required test identities.
        verification = state["verification"]
        report = verification.reports.report

        if (
            classify_test_run(verification) is not TestRunOutcome.PASSED
            or report is None
        ):
            return checked_update(
                state,
                RepairAttemptStatus.VERIFICATION_FAILED,
                ("Post-patch test execution did not pass",),
            )

        try:
            validate_test_case_counts(report)
        except ValueError as error:
            return checked_update(
                state,
                RepairAttemptStatus.VERIFICATION_FAILED,
                (str(error),),
            )

        diagnostics = check_required_tests(report, required_tests)

        # Preserve every baseline test identity and its occurrence count.
        from collections import Counter

        baseline_identities = Counter(
            (case.class_name, case.test_name) for case in baseline_report.test_cases
        )
        verified_identities = Counter(
            (case.class_name, case.test_name) for case in report.test_cases
        )

        if baseline_identities != verified_identities:
            diagnostics += ("Test identities changed between runs",)

        if report.skipped:
            diagnostics += ("Verification contains skipped tests",)

        if diagnostics:
            return checked_update(
                state,
                RepairAttemptStatus.VERIFICATION_FAILED,
                diagnostics,
            )

        return checked_update(state, RepairAttemptStatus.VERIFIED)

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
            )
        }

    def route_after_baseline(state: RepairState) -> str:
        if state.get("status") is not None:
            return "finalize"
        return "collect_diagnosis" if propose_fix is not None else "apply_patch"

    def continue_or_finish(next_node: str):
        def route(state: RepairState) -> str:
            return "finalize" if state.get("status") is not None else next_node

        return route

    builder = StateGraph(RepairState)
    builder.add_node("run_baseline", baseline_node)
    builder.add_node("collect_diagnosis", diagnosis_node)
    builder.add_node("generate_proposal", proposal_node)
    builder.add_node("apply_patch", patch_node)
    builder.add_node("verify", verification_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "run_baseline")
    builder.add_conditional_edges(
        "run_baseline",
        route_after_baseline,
        {name: name for name in ("collect_diagnosis", "apply_patch", "finalize")},
    )
    for node, successor in (
        ("collect_diagnosis", "generate_proposal"),
        ("generate_proposal", "apply_patch"),
        ("apply_patch", "verify"),
    ):
        builder.add_conditional_edges(
            node,
            continue_or_finish(successor),
            {successor: successor, "finalize": "finalize"},
        )
    builder.add_edge("verify", "finalize")
    builder.add_edge("finalize", END)

    # One invocation, no retries, persistence, or shared mutable graph state.
    graph = builder.compile()
    final_state = graph.invoke(
        {
            "request": request,
            "proposal": None,
            "patch": None,
            "verification": None,
            "status": None,
            "diagnostics": (),
        }
    )
    return final_state["result"]
