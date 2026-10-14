import difflib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from repo_doctor.backend_selection import ModelSelection
from repo_doctor.repair import RepairAttemptResult, RepairAttemptStatus
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
        "tests": (
            None
            if report is None
            else {
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
            }
        ),
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
    # History is authoritative when available. A later failed proposal may
    # have no patch even though earlier attempts changed the workspace.
    records = result.attempts if result.attempts else (result,)
    applied = [
        record
        for record in records
        if record.patch is not None and record.patch.status is PatchStatus.APPLIED
    ]
    if not applied:
        return {"status": "not_applied", "content": None}
    if result.status is RepairAttemptStatus.INTEGRITY_FAILED:
        return {
            "status": "unavailable",
            "content": None,
            "diagnostic": "Repair input integrity failed",
        }

    try:
        repository = Repository(result.workspace.project_root)
        # Canonical relative path -> first before hash, latest after hash.
        chains: dict[str, tuple[str, str]] = {}
        for record in applied:
            patch = record.patch
            request = record.request
            if request is None:
                raise ValueError("Applied patch has no retained request")
            target = repository.resolve_path(request.relative_path)
            path = target.relative_to(repository.root).as_posix()
            patch_target = repository.resolve_path(patch.relative_path)
            if patch_target != target:
                raise ValueError("Patch and request targets differ")
            if not patch.before_sha256 or not patch.after_sha256:
                raise ValueError("Applied patch has incomplete hashes")
            if request.expected_sha256 != patch.before_sha256:
                raise ValueError("Request and patch hashes differ")
            if path in chains:
                first_hash, previous_hash = chains[path]
                if patch.before_sha256 != previous_hash:
                    raise ValueError("Applied patch hash chain is broken")
            else:
                first_hash = patch.before_sha256
            chains[path] = (first_hash, patch.after_sha256)

        contents: list[str] = []
        for path, (first_hash, final_hash) in sorted(chains.items()):
            before = _read_diff_bytes(result.workspace.source_root, path)
            after = _read_diff_bytes(result.workspace.project_root, path)
            if (
                sha256(before).hexdigest() != first_hash
                or sha256(after).hexdigest() != final_hash
            ):
                return {
                    "status": "unavailable",
                    "content": None,
                    "diagnostic": "Current files do not match patch hashes",
                }
            contents.append(_unified_diff(before, after, path))
        return {"status": "available", "content": "".join(contents)}
    except Exception as error:
        return {
            "status": "unavailable",
            "content": None,
            "diagnostic": f"Could not construct diff: {type(error).__name__}",
        }


def _patch_summary(patch):
    return (
        None
        if patch is None
        else {
            "status": patch.status.value,
            "relative_path": patch.relative_path,
            "before_sha256": patch.before_sha256,
            "after_sha256": patch.after_sha256,
            "diagnostic": patch.diagnostic,
        }
    )


def _attempt_summary(attempt):
    return {
        "number": attempt.number,
        "status": attempt.status.value,
        "proposal": asdict(attempt.proposal) if attempt.proposal is not None else None,
        "request": asdict(attempt.request) if attempt.request is not None else None,
        "patch": _patch_summary(attempt.patch),
        "verification": _test_run_summary(attempt.verification),
        "diagnostics": list(attempt.diagnostics),
    }


def _regression_summary(result):
    generated = result.regression
    if generated is None:
        return None
    diff = {"status": "unavailable", "content": None}
    if result.status is not RepairAttemptStatus.INTEGRITY_FAILED:
        try:
            original = Repository(result.workspace.source_root).resolve_path(
                generated.relative_path
            )
            after = _read_diff_bytes(
                result.workspace.project_root, generated.relative_path
            )
            if (
                not original.exists()
                and sha256(after).hexdigest() == generated.file_sha256
            ):
                diff = {
                    "status": "available",
                    "content": _unified_diff(
                        b"", after, generated.relative_path
                    ).replace("--- a/" + generated.relative_path, "--- /dev/null", 1),
                }
        except Exception:
            pass
    return {
        "relative_path": generated.relative_path,
        "class_name": generated.class_name,
        "test_name": generated.test_name,
        "sha256": generated.file_sha256,
        "spec": asdict(generated.spec),
        "reproduction_accepted": result.regression_accepted,
        "reproduction": _test_run_summary(result.regression_run),
        "diff": diff,
    }


def build_repair_report(
    result: RepairAttemptResult,
    selection: ModelSelection,
) -> dict:
    patch = result.patch

    return {
        "schema_version": 2,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": result.status.value,
        "backend": selection.backend.value,
        # This records our selection, not independently verified routing.
        "requested_model": selection.model_id,
        "source_root": str(result.workspace.source_root),
        "workspace_root": str(result.workspace.project_root),
        "proposal": (asdict(result.proposal) if result.proposal is not None else None),
        "request": (asdict(result.request) if result.request is not None else None),
        "patch": _patch_summary(patch),
        "attempt_count": len(result.attempts),
        "attempts": [_attempt_summary(attempt) for attempt in result.attempts],
        "diff": _patch_diff(result),
        "regression": _regression_summary(result),
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
