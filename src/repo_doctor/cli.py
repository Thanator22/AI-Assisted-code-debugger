import argparse
from pathlib import Path
from sqlite3 import adapters

from repo_doctor.backend_selection import (
    BACKEND_LABELS, 
    Backend, 
    BackendSelector, 
    choose_number,
    load_selection,
    save_selection,
)
from repo_doctor.model_proposer import ModelProposer
from repo_doctor.models_backend import (
    ClaudeAdapter,
    ClaudeBackendError,
    CopilotAdapter,
    CodexAdapter,
    ModelBackendError,
)
from repo_doctor.repair import RequiredTest, run_fixture_repair
from repo_doctor.tools.build import classify_test_run
from repo_doctor.tools.workspace import create_fixture_workspace
from repo_doctor.repair_reporting import save_repair_report
from repo_doctor.repair_case import load_repair_case
from repo_doctor.benchmark import run_benchmark


# PRODUCTION_PATH = (
#     "src/main/java/com/example/discount/DiscountService.java"
# )
# TEST_PATH = (
#     "src/test/java/com/example/discount/DiscountServiceTest.java"
# )
# REQUIRED_TEST = RequiredTest(
#     class_name="com.example.discount.DiscountServiceTest",
#     test_name="appliesDiscountAtThreshold",
# )


def run_selected_repair(
    selector: BackendSelector,
    fixture: Path,
) -> None:
    selection = selector.selection
    transport = selector.transport

    if selection is None or transport is None:
        print("Choose a backend and model first.")
        return
    case = load_repair_case(fixture)

    workspace = create_fixture_workspace(fixture)
    print(f"\nWorkspace: {workspace.project_root}")
    print(
        f"Using {BACKEND_LABELS[selection.backend]} / "
        f"{selection.model_id}"
    )

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files={case.production_path},
        required_tests=case.required_tests,
        propose_fix=ModelProposer(transport),
        production_path=case.production_path,
        test_path=case.test_path,
    )

    print(f"\nRepair: {result.status.value}")
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair the discount-service fixture."
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
    args = parser.parse_args()

    adapters = {
    Backend.COPILOT: CopilotAdapter(),
    Backend.CLAUDE: ClaudeAdapter(),
    Backend.CODEX: CodexAdapter(),
    }
    selector = BackendSelector(adapters)

    print("Repo Doctor")
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
                    run_selected_repair(selector, args.fixture)
                elif action == 3:
                    if (
                        selector.selection is None
                        or selector.transport is None
                    ):
                        print("Choose a backend and model first.")
                    else:
                        fixtures = tuple(sorted(
                            path.parent
                            for path in args.benchmark_root.glob(
                                "*/repair-case.json"
                            )
                        ))

                        run_benchmark(
                            fixtures=fixtures,
                            selection=selector.selection,
                            transport=selector.transport,
                            output_root=args.results_root,
                        )
                if action in (0, 1) and selector.selection is not None:
                    try:
                        save_selection(selector.selection)
                    except OSError:
                        print(
                            "Selection is active, but could not be saved."
                        )
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