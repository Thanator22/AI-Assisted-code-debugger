"""Bounded heuristic discovery for a single standard Maven Java module.

This is not a Java parser or a permission system. The caller supplies approved
production paths. Discovery selects only from that set and never expands it.
"""

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from repo_doctor.tools.repository import FileReadStatus, Repository

if TYPE_CHECKING:
    from repo_doctor.tools.test_reports import TestIssue

MAIN = "src/main/java/"
TEST = "src/test/java/"
IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
QUALIFIED = rf"{IDENTIFIER}(?:\.{IDENTIFIER})*"
CLASS_NAME = re.compile(rf"{QUALIFIED}\Z")
PACKAGE = re.compile(rf"\bpackage\s+({QUALIFIED})\s*;")
IMPORT = re.compile(rf"\bimport\s+(static\s+)?({QUALIFIED})(\.\*)?\s*;")
FRAME = re.compile(
    rf"^\s*at\s+(?:[^\s/]+/)?({QUALIFIED})\.({IDENTIFIER})"
    rf"\(({IDENTIFIER}\.java):[0-9]+\)",
    re.MULTILINE,
)
NON_CODE = re.compile(
    r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r"|//[^\r\n]*|/\*[\s\S]*?\*/"
)


class DiscoveryError(ValueError):
    """Discovery could not establish one target from complete bounded evidence."""


@dataclass(frozen=True)
class DiscoveryCandidate:
    path: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    test_path: str
    candidates: tuple[DiscoveryCandidate, ...]

    @property
    def production_path(self) -> str:
        if len(self.candidates) != 1:
            raise DiscoveryError("Discovery requires exactly one production candidate")
        return self.candidates[0].path


def production_files(
    repository: Repository, *, max_entries: int = 4000, max_files: int = 200
) -> tuple[str, ...]:
    """Enumerate a bounded application-defined scope, never following symlinks."""
    for name, value in (("max_entries", max_entries), ("max_files", max_files)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    raw = repository.root
    for component in ("src", "main", "java"):
        raw = raw / component
        if raw.is_symlink():
            raise DiscoveryError("Source directory symlinks are unsupported")
    if not raw.is_dir():
        raise DiscoveryError("Standard src/main/java directory is missing")
    pending = [raw]
    files: list[str] = []
    entries = 0
    try:
        while pending:
            with os.scandir(pending.pop()) as children:
                for child in children:
                    entries += 1
                    if entries > max_entries:
                        raise DiscoveryError("Source inventory exceeds entry limit")
                    if child.is_symlink():
                        raise DiscoveryError("Source symlinks are unsupported")
                    if child.is_dir(follow_symlinks=False):
                        pending.append(child.path)
                    elif child.name.endswith(".java"):
                        if not child.is_file(follow_symlinks=False):
                            raise DiscoveryError("Java source is not a regular file")
                        relative = os.path.relpath(child.path, repository.root)
                        path = repository.resolve_path(relative)
                        files.append(path.relative_to(repository.root).as_posix())
                        if len(files) > max_files:
                            raise DiscoveryError("Source inventory exceeds file limit")
    except OSError as error:
        raise DiscoveryError(
            f"Cannot inventory sources: {type(error).__name__}"
        ) from error
    return tuple(sorted(files))


def _code(repository: Repository, path: str) -> str:
    raw = repository.root
    for component in PurePosixPath(path).parts:
        raw = raw / component
        if raw.is_symlink():
            raise DiscoveryError("Evidence symlinks are unsupported")
    result = repository.read_lines(path, 1, repository.max_read_lines + 1)
    if result.status is not FileReadStatus.SUCCESS or result.truncated:
        raise DiscoveryError(
            f"Incomplete discovery evidence: {path} ({result.status.value})"
        )
    # Unicode escapes are processed before Java tokenization. Do not guess.
    if re.search(r"\\u+[0-9A-Fa-f]{4}", result.content):
        raise DiscoveryError("Java Unicode escapes are unsupported in discovery")
    return NON_CODE.sub(
        lambda match: "\n" * match.group().count("\n") + " ", result.content
    )


def _validate_declaration(code: str, relative_type: str) -> None:
    qualified = relative_type[:-5].replace("/", ".")
    package, _, name = qualified.rpartition(".")
    match = PACKAGE.search(code)
    if (match.group(1) if match else "") != package:
        raise DiscoveryError("Java package does not match its source path")
    if not re.search(rf"\b(?:class|interface|enum|record)\s+{re.escape(name)}\b", code):
        raise DiscoveryError("Java type declaration does not match its source path")


def discover_repair_target(
    repository: Repository, issue: "TestIssue", allowed_files: set[str]
) -> DiscoveryResult:
    """Resolve a test and candidates using frames and lexical type references.

    All evidence is heuristic. Multiple candidates remain ambiguous even when
    one has more reasons. Only a unique approved candidate may be selected.
    """
    if len(issue.class_name) > 500 or not CLASS_NAME.fullmatch(issue.class_name):
        raise DiscoveryError("Unsupported failing test class name")
    if len(issue.stack_trace) > 6000:
        raise DiscoveryError("Stack trace exceeds discovery limit")
    if len(allowed_files) > 200:
        raise DiscoveryError("Approved source set exceeds discovery limit")
    outer = issue.class_name.split("$", 1)[0]
    test_path = TEST + outer.replace(".", "/") + ".java"
    code = _code(repository, test_path)
    _validate_declaration(code, test_path[len(TEST) :])
    inventory = production_files(repository)
    approved = {
        repository.resolve_path(path).relative_to(repository.root).as_posix()
        for path in allowed_files
    }
    by_type = {path[len(MAIN) : -5].replace("/", "."): path for path in inventory}
    reasons: dict[str, set[str]] = {}

    def add(qualified: str, reason: str) -> None:
        path = by_type.get(qualified.split("$", 1)[0])
        if path:
            reasons.setdefault(path, set()).add(reason)

    for frame in FRAME.finditer(issue.stack_trace):
        owner = frame.group(1).split("$", 1)[0]
        if frame.group(3) == owner.rsplit(".", 1)[-1] + ".java":
            add(owner, "Production frame in current failure stack")

    imports = list(IMPORT.finditer(code))
    body = PACKAGE.sub(" ", IMPORT.sub(" ", code))
    tokens = set(re.findall(IDENTIFIER, body))
    package = outer.rpartition(".")[0]
    explicit: dict[str, str] = {}
    wildcards: set[str] = set()
    for item in imports:
        static, qualified, wildcard = item.groups()
        if static:
            if wildcard:
                add(qualified, "Static wildcard import in failing test")
            else:
                owner, _, member = qualified.rpartition(".")
                if member in tokens:
                    add(owner, "Referenced static import in failing test")
        elif wildcard:
            wildcards.add(qualified)
        else:
            explicit[qualified.rsplit(".", 1)[-1]] = qualified

    for qualified, path in by_type.items():
        owner_package, _, simple = qualified.rpartition(".")
        if (
            re.search(rf"(?<![\w$]){re.escape(qualified)}(?![\w$])", body)
            and "." in qualified
        ):
            add(qualified, "Fully qualified type reference in failing test")
        if simple not in tokens:
            continue
        if simple in explicit:
            if explicit[simple] == qualified:
                add(qualified, "Explicit imported type referenced in failing test")
        elif owner_package == package or owner_package in wildcards:
            add(qualified, "Same-package or wildcard type reference in failing test")

    # Evidence outside approval must not silently disappear and make another
    # candidate appear unique. Permission checking happens after discovery.
    if not reasons:
        raise DiscoveryError("No production candidate found in test or failure stack")
    if len(reasons) > 10:
        raise DiscoveryError("Discovery exceeds candidate limit")
    candidates = tuple(
        DiscoveryCandidate(path, tuple(sorted(why)))
        for path, why in sorted(reasons.items())
    )
    if len(candidates) != 1:
        raise DiscoveryError("Ambiguous production candidates: " + ", ".join(reasons))
    if candidates[0].path not in approved:
        raise DiscoveryError("Discovered production file is not approved for repair")
    source = _code(repository, candidates[0].path)
    _validate_declaration(source, candidates[0].path[len(MAIN) :])
    return DiscoveryResult(test_path=test_path, candidates=candidates)
