"""Loading of the concept-type registry (config/wiki-types.yaml)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

_TYPE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class ConceptType:
    name: str
    folder: PurePosixPath
    description: str
    managed: bool = False
    label: str = ""
    private: bool = False  # new pages of this type are private unless the write says otherwise
    written_by: str = ""  # a managed type: who writes its pages (told to agents)

    @property
    def heading(self) -> str:
        return self.label or self.folder.name.replace("-", " ").capitalize() or self.name


class TypeRegistry:
    def __init__(self, types: list[ConceptType]) -> None:
        if not types:
            raise ValueError("type registry is empty")
        self._by_name = {t.name: t for t in types}

    @classmethod
    def load(cls, path: Path) -> TypeRegistry:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_types = data.get("types")
        if not isinstance(raw_types, dict):
            raise ValueError(f"{path}: expected a top-level 'types' mapping")
        types: list[ConceptType] = []
        for name, spec in raw_types.items():
            if not isinstance(name, str) or not _TYPE_NAME.match(name):
                raise ValueError(f"{path}: invalid type name {name!r}")
            if not isinstance(spec, dict) or not isinstance(spec.get("folder"), str):
                raise ValueError(f"{path}: type {name!r} needs a 'folder'")
            folder = PurePosixPath(spec["folder"])
            if folder.is_absolute() or ".." in folder.parts:
                raise ValueError(f"{path}: type {name!r} has an unsafe folder")
            types.append(
                ConceptType(
                    name=name,
                    folder=folder,
                    description=str(spec.get("description", "")).strip(),
                    managed=bool(spec.get("managed", False)),
                    label=str(spec.get("label", "")).strip(),
                    private=spec.get("private", False) is True,
                    written_by=str(spec.get("written_by", "")).strip(),
                )
            )
        return cls(types)

    def get(self, name: str) -> ConceptType | None:
        return self._by_name.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def all(self) -> list[ConceptType]:
        return list(self._by_name.values())

    def writable(self) -> list[ConceptType]:
        """Types agents may create with write_concept."""
        return [t for t in self._by_name.values() if not t.managed]

    def read_only(self) -> list[ConceptType]:
        """Types only the server writes (managed): agents read them, never write them."""
        return [t for t in self._by_name.values() if t.managed]

    def for_path(self, rel: PurePosixPath) -> ConceptType | None:
        """The type whose folder directly or indirectly contains `rel` (longest match)."""
        best: ConceptType | None = None
        for t in self._by_name.values():
            if t.folder == PurePosixPath("."):
                if len(rel.parts) == 1 and best is None:
                    best = t
                continue
            if rel.parts[: len(t.folder.parts)] == t.folder.parts and (
                best is None or len(t.folder.parts) > len(best.folder.parts)
            ):
                best = t
        return best
