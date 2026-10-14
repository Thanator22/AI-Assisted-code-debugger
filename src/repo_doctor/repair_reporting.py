import difflib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from repo_doctor.backend_selection import ModelSelection
from repo_doctor.repair import RepairAttemptResult
from repo_doctor.tools.build import MavenTestResult, classify_test_run
from repo_doctor.tools.patching import PatchStatus
from repo_doctor.tools.repository import Repository


MAX_DIFF_FILE_BYTES = 1_000_000


def _test_run_summary(run: MavenTestResult | None):
    if run is None:
        return None

    report = run.reports.report

    return {
        "outcome": classify_test_run(run).value,
        "execution": {
            "status": run.execution.status.value,
            "exit_code": run.execution.exit_code,
            "duration_seconds": run.execution.duration_seconds,
            "output_truncated": run.execution.output_truncated,
        },
        "report_status": run.reports.status.value,
        "report_directory": str(run.report_directory),
        "report_diagnostics": list(run.reports.diagnostics),
        "tests": None if report is None else {
            "detected": report.total_tests,
            "executed": report.executed_tests,
            "passed": report.passed_tests,
            "failures": report.failures,
            "errors": report.errors,
            "skipped": report.skipped,
            "cases": [
                {
                    "class_name": case.class_name,
                    "test_name": case.test_name,
                    "status": case.status.value,
                }
                for case in report.test_cases
            ],
        },
    }


def _read_diff_bytes(root: Path, relative_path: str) -> bytes:
    path = Repository(root).resolve_path(relative_path)

    with path.open("rb") as source:
        data = source.read(MAX_DIFF_FILE_BYTES + 1)

    if len(data) > MAX_DIFF_FILE_BYTES:
        raise ValueError("File exceeds diff size limit")

    return data


def _unified_diff(before: bytes, after: bytes, path: str) -> str:
    lines = difflib.unified_diff(
        before.decode("utf-8").splitlines(keepends=True),
        after.decode("utf-8").splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="\n",
    )

    output: list[str] = []

    for line in lines:
        output.append(line)

        # Preserve the distinction between a final newline and no newline.
        if not line.endswith(("\n", "\r")):
            output.append("\n\\ No newline at end of file\n")

    return "".join(output)


def _patch_diff(result: RepairAttemptResult) -> dict:
    patch = result.patch
    request = result.request

    if patch is None or patch.status is not PatchStatus.APPLIED:
        return {"status": "not_applied", "content": None}

    if request is None:
        return {
            "status": "unavailable",
            "content": None,
            "diagnostic": "Applied patch has no retained request",
        }

    try:
        before = _read_diff_bytes(
            result.workspace.source_root,
            request.relative_path,
        )
        after = _read_diff_bytes(
            result.workspace.project_root,
            request.relative_path,
        )

        # Do not present a later file change as the patch we applied.
        if (
            sha256(before).hexdigest() != patch.before_sha256
            or sha256(after).hexdigest() != patch.after_sha256
        ):
            return {
                "status": "unavailable",
                "content": None,
                "diagnostic": "Current files do not match patch hashes",
            }

        return {
            "status": "available",
            "content": _unified_diff(before, after, request.relative_path),
        }
    except Exception as error:
        return {
            "status": "unavailable",
            "content": None,
            "diagnostic": f"Could not construct diff: {type(error).__name__}",
        }


def build_repair_report(
    result: RepairAttemptResult,
    selection: ModelSelection,
) -> dict:
    patch = result.patch

    return {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": result.status.value,
        "backend": selection.backend.value,
        # This records our selection, not independently verified routing.
        "requested_model": selection.model_id,
        "source_root": str(result.workspace.source_root),
        "workspace_root": str(result.workspace.project_root),
        "proposal": (
            asdict(result.proposal)
            if result.proposal is not None
            else None
        ),
        "request": (
            asdict(result.request)
            if result.request is not None
            else None
        ),
        "patch": None if patch is None else {
            "status": patch.status.value,
            "relative_path": patch.relative_path,
            "before_sha256": patch.before_sha256,
            "after_sha256": patch.after_sha256,
            "diagnostic": patch.diagnostic,
        },
        "diff": _patch_diff(result),
        "baseline": _test_run_summary(result.baseline),
        "verification": _test_run_summary(result.verification),
        "diagnostics": list(result.diagnostics),
    }


def save_repair_report(
    result: RepairAttemptResult,
    selection: ModelSelection,
) -> Path:
    payload = build_repair_report(result, selection)

    directory = result.workspace.project_root / "target" / "repo-doctor"
    directory.mkdir(parents=True, exist_ok=True)

    destination = directory / "repair-report.json"
    temporary = directory / "repair-report.json.tmp"

    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    return destination