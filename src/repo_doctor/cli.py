import argparse
from pathlib import Path
from sqlite3 import adapters

from repo_doctor.backend_selection import (
    BACKEND_LABELS, 
    Backend, 
    BackendSelector, 
    choose_number
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


PRODUCTION_PATH = (
    "src/main/java/com/example/discount/DiscountService.java"
)
TEST_PATH = (
    "src/test/java/com/example/discount/DiscountServiceTest.java"
)
REQUIRED_TEST = RequiredTest(
    class_name="com.example.discount.DiscountServiceTest",
    test_name="appliesDiscountAtThreshold",
)


def run_selected_repair(
    selector: BackendSelector,
    fixture: Path,
) -> None:
    selection = selector.selection
    transport = selector.transport

    if selection is None or transport is None:
        print("Choose a backend and model first.")
        return

    workspace = create_fixture_workspace(fixture)
    print(f"\nWorkspace: {workspace.project_root}")
    print(
        f"Using {BACKEND_LABELS[selection.backend]} / "
        f"{selection.model_id}"
    )

    result = run_fixture_repair(
        workspace,
        request=None,
        allowed_files={PRODUCTION_PATH},
        required_tests=(REQUIRED_TEST,),
        propose_fix=ModelProposer(transport),
        production_path=PRODUCTION_PATH,
        test_path=TEST_PATH,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair the discount-service fixture."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("benchmarks/fixtures/discount-service"),
    )
    args = parser.parse_args()

    adapters = {
    Backend.COPILOT: CopilotAdapter(),
    Backend.CLAUDE: ClaudeAdapter(),
    Backend.CODEX: CodexAdapter(),
    }
    selector = BackendSelector(adapters)

    print("Repo Doctor")
    print("Available backend: Claude")
    print(
        "Running a repair sends diagnosis excerpts to the selected "
        "backend and may incur API charges."
    )

    try:
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
                    "Exit",
                ],
            )

            if action == 3:
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