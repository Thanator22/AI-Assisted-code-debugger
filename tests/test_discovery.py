from types import SimpleNamespace

import pytest

from repo_doctor.discovery import (
    DiscoveryError,
    discover_repair_target,
    production_files,
)
from repo_doctor.tools.repository import Repository

MAIN = "src/main/java/"
TEST = "src/test/java/"


def put(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def issue(class_name="example.ServiceTest", stack_trace=""):
    return SimpleNamespace(class_name=class_name, stack_trace=stack_trace)


def setup_sources(root, test_body="Service service = new Service();", imports=""):
    put(root, MAIN + "example/Service.java", "package example; public class Service {}")
    put(
        root,
        TEST + "example/ServiceTest.java",
        f"package example; {imports}\nclass ServiceTest {{ {test_body} }}",
    )
    return Repository(root)


def test_same_package_reference_discovers_target(tmp_path):
    repo = setup_sources(tmp_path)
    result = discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})
    assert result.production_path == MAIN + "example/Service.java"
    assert result.test_path == TEST + "example/ServiceTest.java"
    assert result.candidates[0].reasons


@pytest.mark.parametrize(
    "imports, body",
    [
        ("import other.Helper;", "Helper value = new Helper();"),
        ("import other.*;", "Helper value = new Helper();"),
        ("", "other.Helper value = new other.Helper();"),
        ("import static other.Helper.check;", "void test() { check(); }"),
    ],
)
def test_import_and_qualified_references(tmp_path, imports, body):
    repo = setup_sources(tmp_path, body, imports)
    put(tmp_path, MAIN + "other/Helper.java", "package other; class Helper {}")
    result = discover_repair_target(repo, issue(), {MAIN + "other/Helper.java"})
    assert result.production_path == MAIN + "other/Helper.java"


def test_failure_stack_can_locate_production_source(tmp_path):
    repo = setup_sources(tmp_path, "void test() {}")
    result = discover_repair_target(
        repo,
        issue(
            stack_trace=(
                "at example.Service.calculate(Service.java:12)\n"
                "at example.ServiceTest.test(ServiceTest.java:20)"
            )
        ),
        {MAIN + "example/Service.java"},
    )
    assert result.production_path == MAIN + "example/Service.java"


def test_nested_test_uses_outer_source_file(tmp_path):
    repo = setup_sources(tmp_path)
    result = discover_repair_target(
        repo, issue("example.ServiceTest$Nested"), {MAIN + "example/Service.java"}
    )
    assert result.test_path == TEST + "example/ServiceTest.java"


def test_comments_and_strings_are_not_type_evidence(tmp_path):
    repo = setup_sources(tmp_path, 'String text = "Service"; /* new Service(); */')
    with pytest.raises(DiscoveryError, match="No production candidate"):
        discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})


def test_unused_import_is_not_type_evidence(tmp_path):
    repo = setup_sources(tmp_path, "void test() {}", "import example.Service;")
    with pytest.raises(DiscoveryError, match="No production candidate"):
        discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})


def test_multiple_candidates_are_not_resolved_by_allowlist(tmp_path):
    repo = setup_sources(tmp_path, "Service service; Helper helper;")
    put(tmp_path, MAIN + "example/Helper.java", "package example; class Helper {}")
    with pytest.raises(DiscoveryError, match="Ambiguous"):
        discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})


def test_discovery_cannot_expand_permissions(tmp_path):
    repo = setup_sources(tmp_path)
    with pytest.raises(DiscoveryError, match="not approved"):
        discover_repair_target(repo, issue(), set())


def test_explicit_import_prevents_same_name_collision(tmp_path):
    repo = setup_sources(tmp_path, "Service service;", "import other.Service;")
    put(tmp_path, MAIN + "other/Service.java", "package other; class Service {}")
    result = discover_repair_target(repo, issue(), {MAIN + "other/Service.java"})
    assert result.production_path == MAIN + "other/Service.java"


def test_package_mismatch_is_rejected(tmp_path):
    repo = setup_sources(tmp_path)
    put(tmp_path, MAIN + "example/Service.java", "package wrong; class Service {}")
    with pytest.raises(DiscoveryError, match="package"):
        discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})


def test_truncated_test_evidence_is_rejected(tmp_path):
    setup_sources(tmp_path)
    repo = Repository(tmp_path, max_output_characters=10)
    with pytest.raises(DiscoveryError, match="Incomplete"):
        discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})


def test_line_limit_is_detected_even_when_default_range_would_hide_tail(tmp_path):
    setup_sources(tmp_path)
    put(
        tmp_path,
        TEST + "example/ServiceTest.java",
        "package example;\nclass ServiceTest {\nService service;\n}\n",
    )
    repo = Repository(tmp_path, max_read_lines=2)
    with pytest.raises(DiscoveryError, match="Incomplete"):
        discover_repair_target(repo, issue(), {MAIN + "example/Service.java"})


@pytest.mark.parametrize(
    "name", ["../ServiceTest", "/ServiceTest", "example/ServiceTest", ""]
)
def test_invalid_class_names_are_rejected(tmp_path, name):
    repo = setup_sources(tmp_path)
    with pytest.raises(DiscoveryError, match="class name"):
        discover_repair_target(repo, issue(name), {MAIN + "example/Service.java"})


def test_inventory_limit_does_not_return_partial_files(tmp_path):
    repo = setup_sources(tmp_path)
    put(tmp_path, MAIN + "example/Other.java", "package example; class Other {}")
    with pytest.raises(DiscoveryError, match="file limit"):
        production_files(repo, max_files=1)


def test_inventory_entry_limit(tmp_path):
    repo = setup_sources(tmp_path)
    with pytest.raises(DiscoveryError, match="entry limit"):
        production_files(repo, max_entries=1)


def test_symlink_is_rejected(tmp_path):
    repo = setup_sources(tmp_path)
    link = tmp_path / MAIN / "example/Link.java"
    try:
        link.symlink_to(tmp_path / MAIN / "example/Service.java")
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    with pytest.raises(DiscoveryError, match="symlink"):
        production_files(repo)
