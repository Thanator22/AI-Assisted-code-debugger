import argparse
from pathlib import Path

from repo_doctor.backend_selection import (
    BACKEND_LABELS,
    Backend,
    BackendSelector,
    choose_number,
    load_selection,
    save_selection,
)
from repo_doctor.model_proposer import ModelProposer
from repo_doctor.regression import ModelRegressionGenerator
from repo_doctor.models_backend import (
    ClaudeAdapter,
    ClaudeBackendError,
    CopilotAdapter,
    CodexAdapter,
    ModelBackendError,
)
from repo_doctor.repair import RetryPolicy, run_fixture_repair
from repo_doctor.tools.build import classify_test_run
from repo_doctor.tools.workspace import create_fixture_workspace
from repo_doctor.repair_reporting import save_repair_report
from repo_doctor.repair_case import load_repair_case
from repo_doctor.benchmark import run_benchmark


def run_selected_repair(
    selector: BackendSelector,
    fixture: Path,
    *,
    retry_policy: RetryPolicy | None = None,
    generate_regression: bool = False,
) -> None:
    policy = retry_policy if retry_policy is not None else RetryPolicy()
    selection = selector.selection
    transport = selector.transport

    if selection is None or transport is None:
        print("Choose a backend and model first.")
        return
    case = load_repair_case(fixture)

    workspace = create_fixture_workspace(fixture)
    print(f"\nWorkspace: {workspace.project_root}")
    print(f"Using {BACKEND_LABELS[selection.backend]} / " f"{selection.model_id}")

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files=(
            {case.production_path}
            if case.production_path is not None
            else set(case.allowed_files)
        ),
        required_tests=case.required_tests,
        propose_fix=ModelProposer(transport),
        **(
            {"generate_regression": ModelRegressionGenerator(transport)}
            if generate_regression
            else {}
        ),
        production_path=case.production_path,
        test_path=case.test_path,
        retry_policy=policy,
    )

    print(f"\nRepair: {result.status.value}")
    if getattr(result, "regression", None) is not None:
        print(
            "Regression reproduction:",
            "accepted" if result.regression_accepted else "rejected",
        )
        print(f"Generated test: {result.regression.relative_path}")
    print(f"Attempts: {len(result.attempts)}/{policy.max_attempts}")
    print(f"Baseline: {classify_test_run(result.baseline).value}")
    print(
        "Patch:",
        result.patch.status.value if result.patch else "not attempted",
    )

    if result.verification is not None:
        verification = result.verification
        print(f"Verification: {classify_test_run(verification).value}")

        report = verification.reports.report
        if report is not None:
            print(f"Detected: {report.total_tests}")
            print(f"Executed: {report.executed_tests}")
            print(f"Passed: {report.passed_tests}")

    for diagnostic in result.diagnostics:
        print(f"Diagnostic: {diagnostic}")
    try:
        report_path = save_repair_report(result, selection)
    except Exception as error:
        print(
            "Repair result shown above, but saving its report failed: "
            f"{type(error).__name__}"
        )
    else:
        print(f"Saved report: {report_path}")


def positive_attempt_count(value: str) -> int:
    try:
        return RetryPolicy(max_attempts=int(value)).max_attempts
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(
            "max-attempts must be a positive integer"
        ) from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair Java fixtures and run repair benchmarks."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("benchmarks/fixtures/discount-service"),
    )
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("benchmarks/fixtures"),
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("benchmarks/results"),
    )
    parser.add_argument(
        "--max-attempts",
        type=positive_attempt_count,
        default=1,
        help="Maximum total attempts per fixture (default: 1).",
    )
    parser.add_argument(
        "--generate-regression",
        action="store_true",
        help="Require a generated numeric JUnit regression to fail before repair and pass afterward.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    policy = RetryPolicy(max_attempts=args.max_attempts)

    adapters = {
        Backend.COPILOT: CopilotAdapter(),
        Backend.CLAUDE: ClaudeAdapter(),
        Backend.CODEX: CodexAdapter(),
    }
    selector = BackendSelector(adapters)

    print("Repo Doctor")
    print(f"Maximum attempts per fixture: {policy.max_attempts}")
    print("Available backends: GitHub Copilot, Claude, ChatGPT / Codex")
    print(
        "Running a repair sends diagnosis excerpts to the selected "
        "backend and may incur API charges."
    )

    try:
        try:
            saved = load_selection()
            if saved is not None:
                print("Restoring saved backend and model...")
                selector.restore(saved)
        except Exception as error:
            print(
                "Could not restore the saved selection: "
                f"{type(error).__name__}. Choose it from the menu."
            )
        while True:
            if selector.selection is None:
                print("\nCurrent selection: none")
            else:
                selection = selector.selection
                print(
                    "\nCurrent selection: "
                    f"{BACKEND_LABELS[selection.backend]} / "
                    f"{selection.model_id}"
                )

            action = choose_number(
                "Choose action: ",
                [
                    "Choose backend and model",
                    "Switch model",
                    "Run fixture repair",
                    "Run all benchmark fixtures",
                    "Exit",
                ],
            )

            if action == 4:
                break

            try:
                if action == 0:
                    selector.configure()
                elif action == 1:
                    if selector.selection is None:
                        print("Choose a backend first.")
                    else:
                        selector.configure(switch_model_only=True)
                elif action == 2:
                    run_selected_repair(
                        selector,
                        args.fixture,
                        retry_policy=policy,
                        **(
                            {"generate_regression": True}
                            if args.generate_regression
                            else {}
                        ),
                    )
                elif action == 3:
                    if selector.selection is None or selector.transport is None:
                        print("Choose a backend and model first.")
                    else:
                        fixtures = tuple(
                            sorted(
                                path.parent
                                for path in args.benchmark_root.glob(
                                    "*/repair-case.json"
                                )
                            )
                        )

                        run_benchmark(
                            fixtures=fixtures,
                            selection=selector.selection,
                            transport=selector.transport,
                            output_root=args.results_root,
                            retry_policy=policy,
                            **(
                                {"generate_regression": True}
                                if args.generate_regression
                                else {}
                            ),
                        )
                if action in (0, 1) and selector.selection is not None:
                    try:
                        save_selection(selector.selection)
                    except OSError:
                        print("Selection is active, but could not be saved.")
            except (ModelBackendError, ClaudeBackendError) as error:
                print(f"Action failed: {error}")
            except Exception as error:
                # Avoid displaying raw SDK errors or credential-bearing data.
                print(f"Action failed: {type(error).__name__}")
                print("The action did not complete successfully.")

    except (KeyboardInterrupt, EOFError):
        print("\nExiting.")
    finally:
        for adapter in adapters.values():
            try:
                adapter.close()
            except Exception as error:
                print(f"Backend cleanup failed: {type(error).__name__}")


if __name__ == "__main__":
    main()
