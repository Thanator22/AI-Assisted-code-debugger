import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from repo_doctor.backend_selection import ModelSelection, Transport
from repo_doctor.model_proposer import ModelProposer
from repo_doctor.repair import run_fixture_repair
from repo_doctor.repair_case import load_repair_case
from repo_doctor.repair_reporting import save_repair_report
from repo_doctor.tools.workspace import create_fixture_workspace


@dataclass(frozen=True)
class BenchmarkRow:
    fixture: str
    outcome: str
    duration_seconds: float
    workspace: str | None
    report_path: str | None
    diagnostic: str = ""


def benchmark_fixture(
    fixture: Path,
    selection: ModelSelection,
    transport: Transport,
) -> BenchmarkRow:
    started = perf_counter()
    workspace = None

    try:
        case = load_repair_case(fixture)
        workspace = create_fixture_workspace(fixture)

        result = run_fixture_repair(
            workspace,
            request=None,
            allowed_files={case.production_path},
            required_tests=case.required_tests,
            propose_fix=ModelProposer(transport),
            production_path=case.production_path,
            test_path=case.test_path,
        )
    except Exception as error:
        return BenchmarkRow(
            fixture=str(fixture.resolve()),
            outcome="runner_error",
            duration_seconds=perf_counter() - started,
            workspace=(
                str(workspace.project_root)
                if workspace is not None
                else None
            ),
            report_path=None,
            diagnostic=f"Attempt raised {type(error).__name__}",
        )

    report_path = None
    diagnostic = ""

    try:
        report_path = str(save_repair_report(result, selection))
    except Exception as error:
        # Reporting failure must not overwrite the repair outcome.
        diagnostic = f"Report save failed: {type(error).__name__}"

    return BenchmarkRow(
        fixture=str(fixture.resolve()),
        outcome=result.status.value,
        duration_seconds=perf_counter() - started,
        workspace=str(workspace.project_root),
        report_path=report_path,
        diagnostic=diagnostic,
    )


def _write_summary(
    path: Path,
    selection: ModelSelection,
    fixtures: tuple[Path, ...],
    rows: list[BenchmarkRow],
) -> None:
    verified = sum(row.outcome == "verified" for row in rows)

    payload = {
        "schema_version": 1,
        "backend": selection.backend.value,
        "requested_model": selection.model_id,
        "planned_fixtures": [str(path.resolve()) for path in fixtures],
        "planned_count": len(fixtures),
        "completed_count": len(rows),
        "complete": len(rows) == len(fixtures),
        "verified_count": verified,
        "verified_fraction_of_planned": verified / len(fixtures),
        "results": [asdict(row) for row in rows],
    }

    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_benchmark(
    fixtures: tuple[Path, ...],
    selection: ModelSelection,
    transport: Transport,
    output_root: Path,
    *,
    write: Callable[[str], None] = print,
) -> Path:
    if not fixtures:
        raise ValueError("Specify at least one fixture")

    resolved = tuple(path.resolve() for path in fixtures)
    if len(set(resolved)) != len(resolved):
        raise ValueError("Duplicate fixture paths are not allowed")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = output_root / f"{timestamp}-{uuid4().hex[:8]}"
    run_directory.mkdir(parents=True, exist_ok=False)
    summary_path = run_directory / "summary.json"

    rows: list[BenchmarkRow] = []
    _write_summary(summary_path, selection, fixtures, rows)

    for fixture in fixtures:
        write(f"\nRunning: {fixture.name}")

        row = benchmark_fixture(fixture, selection, transport)
        rows.append(row)

        # Checkpoint after every attempt, including failed attempts.
        _write_summary(summary_path, selection, fixtures, rows)

        write(
            f"{fixture.name}: {row.outcome} "
            f"({row.duration_seconds:.1f}s)"
        )
        if row.diagnostic:
            write(row.diagnostic)

    verified = sum(row.outcome == "verified" for row in rows)
    write(f"\nVerified: {verified}/{len(fixtures)}")
    write(f"Summary: {summary_path}")

    return summary_path