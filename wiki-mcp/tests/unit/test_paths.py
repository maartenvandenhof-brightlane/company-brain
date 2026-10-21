from pathlib import Path, PurePosixPath

import pytest

from wiki_mcp.wiki.paths import (
    PathError,
    bundle_link,
    check_concept_path,
    doc_id,
    normalize_ref,
    resolve_in_bundle,
    slugify,
)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("wiki/people/jane", "wiki/people/jane.md"),
        ("wiki/people/jane.md", "wiki/people/jane.md"),
        ("/wiki/people/jane.md", "wiki/people/jane.md"),
        ("  wiki/people/jane  ", "wiki/people/jane.md"),
        ("wiki/people/jane.md#section", "wiki/people/jane.md"),
        ("AGENTS", "AGENTS.md"),
    ],
)
def test_normalize_ref_accepts(ref: str, expected: str) -> None:
    assert str(normalize_ref(ref)) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "../etc/passwd",
        "wiki/../../etc/passwd",
        "wiki/./x",
        "wiki\\people\\x",
        "wiki/people/x\x00",
        ".git/config",
        "wiki/.hidden/x",
        "https://evil.example/x.md",
        "//evil/x",
        "a/b/c/d/e/f/g.md",
        "wiki/pe ople/x;rm -rf",
        "x" * 500,
    ],
)
def test_normalize_ref_rejects(ref: str) -> None:
    with pytest.raises(PathError):
        normalize_ref(ref)


def test_doc_id_and_link() -> None:
    rel = PurePosixPath("wiki/people/jane.md")
    assert doc_id(rel) == "wiki/people/jane"
    assert bundle_link(rel) == "/wiki/people/jane.md"


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "wiki" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathError):
        resolve_in_bundle(root, PurePosixPath("wiki/link/x.md"))


def test_resolve_ok(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    path = resolve_in_bundle(root, PurePosixPath("wiki/people/x.md"))
    assert path == root.resolve() / "wiki/people/x.md"


def test_check_concept_path(registry) -> None:  # type: ignore[no-untyped-def]
    assert (
        check_concept_path(normalize_ref("wiki/people/jane-doe"), registry, "person").name
        == "person"
    )
    with pytest.raises(PathError, match="must live under wiki/people"):
        check_concept_path(normalize_ref("wiki/projects/jane"), registry, "person")
    with pytest.raises(PathError, match="unknown type"):
        check_concept_path(normalize_ref("wiki/people/jane"), registry, "alien")
    with pytest.raises(PathError, match="managed"):
        check_concept_path(normalize_ref("raw/x"), registry, "source")
    with pytest.raises(PathError, match="invalid slug"):
        check_concept_path(normalize_ref("wiki/people/Jane Doe"), registry, "person")
    with pytest.raises(PathError, match="reserved"):
        check_concept_path(normalize_ref("wiki/people/index"), registry, "person")
    with pytest.raises(PathError):
        check_concept_path(normalize_ref("wiki/people"), registry, "person")


def test_slugify() -> None:
    assert slugify("Hello, World! Ça va?") == "hello-world-a-va"
    assert slugify("!!!") == "untitled"
