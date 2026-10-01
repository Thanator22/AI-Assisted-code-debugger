from pathlib import Path
import pytest
from hashlib import sha256

from repo_doctor.tools.patching import (
    PatchResult,
    ReplacementRequest,
    PatchApplier,
    PatchStatus,
)

from repo_doctor.tools.workspace import RepairWorkspace


@pytest.fixture
def workspace(tmp_path: Path) -> RepairWorkspace:
    source = tmp_path / "source"
    destination = tmp_path / "repair"
    source.mkdir()
    destination.mkdir()

    return RepairWorkspace(
        source_root=source,
        project_root=destination,
    )


def test_resolves_approved_production_file(workspace):
    relative = "src/main/java/Example.java"
    target = workspace.project_root / relative
    target.parent.mkdir(parents=True)
    target.write_text("class Example {}", encoding="utf-8")

    applier = PatchApplier(workspace, allowed_files={relative})

    assert applier._resolve_target(relative) == target.resolve()


@pytest.mark.parametrize(
    "relative, expected",
    [
        ("../outside.java", PatchStatus.INVALID_PATH),
        ("pom.xml", PatchStatus.NOT_ALLOWED),
        ("src/test/java/ExampleTest.java", PatchStatus.NOT_ALLOWED),
        ("src/main/java/Other.java", PatchStatus.NOT_ALLOWED),
    ],
)
def test_rejects_unapproved_targets(workspace, relative, expected):
    applier = PatchApplier(
        workspace,
        allowed_files={"src/main/java/Example.java"},
    )

    result = applier._resolve_target(relative)

    assert result.status is expected


def test_rejects_original_as_repair_workspace(workspace):
    overlapping = RepairWorkspace(
        source_root=workspace.source_root,
        project_root=workspace.source_root,
    )

    with pytest.raises(ValueError, match="separate"):
        PatchApplier(overlapping, allowed_files=set())
        
@pytest.mark.parametrize(
    "content, old, new, stale, expected",
    [
        (b"quantity > 10", ">", ">=", True, PatchStatus.STALE_CONTENT),
        (b"quantity > 10", "<", "<=", False, PatchStatus.MATCH_NOT_FOUND),
        (b"a > b > c", ">", ">=", False, PatchStatus.AMBIGUOUS_MATCH),
        (b"aaa", "aa", "b", False, PatchStatus.AMBIGUOUS_MATCH),
    ],
)
def test_prepare_rejects_without_changing_file(
    workspace, content, old, new, stale, expected
):
    relative = "src/main/java/Example.java"
    target = workspace.project_root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)

    expected_hash = sha256(
        b"earlier version" if stale else content
    ).hexdigest()

    request = ReplacementRequest(
        relative_path=relative,
        expected_sha256=expected_hash,
        old_text=old,
        new_text=new,
    )

    result = PatchApplier(
        workspace, allowed_files={relative}
    ).apply(request)

    assert isinstance(result, PatchResult)
    assert result.status is expected
    assert result.after_sha256 is None
    assert target.read_bytes() == content


def test_prepare_preserves_line_endings_without_writing(workspace):
    relative = "src/main/java/Example.java"
    target = workspace.project_root / relative
    target.parent.mkdir(parents=True)

    before = b"if (quantity > 10) {\r\n    applyDiscount();\r\n}\r\n"
    expected = b"if (quantity >= 10) {\r\n    applyDiscount();\r\n}\r\n"
    target.write_bytes(before)

    request = ReplacementRequest(
        relative_path=relative,
        expected_sha256=sha256(before).hexdigest(),
        old_text="quantity > 10",
        new_text="quantity >= 10",
    )

    result = PatchApplier(
        workspace, allowed_files={relative}
    )._prepare(request)

    assert not isinstance(result, PatchResult)
    assert result.before == before
    assert result.after == expected
    assert result.after_sha256 == sha256(expected).hexdigest()
    assert target.read_bytes() == before