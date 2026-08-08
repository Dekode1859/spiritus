"""Packaged Spiritus skill definitions for OpenCode discovery."""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .permissions import Access

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    instructions: str
    access: Access | str = Access.ALLOW
    license: str = ""
    compatibility: str = "opencode"
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if len(name) > 64 or not _SKILL_NAME.fullmatch(name):
            raise ValueError(
                "skill name must be lowercase alphanumeric words separated by single '-'"
            )
        description = self.description.strip()
        if not description or len(description) > 1024:
            raise ValueError("skill description must contain 1 to 1024 characters")
        instructions = self.instructions.strip()
        if not instructions:
            raise ValueError("skill instructions cannot be empty")
        metadata = {str(key): str(value) for key, value in self.metadata.items()}
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "instructions", instructions)
        object.__setattr__(self, "access", Access.parse(self.access))
        object.__setattr__(self, "license", self.license.strip())
        object.__setattr__(self, "compatibility", self.compatibility.strip())
        object.__setattr__(self, "metadata", metadata)

    def compile(self, project_root: Path) -> Path:
        directory = Path(project_root) / ".opencode" / "skills" / self.name
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "SKILL.md"
        frontmatter = [
            "---",
            f"name: {self.name}",
            f"description: {json.dumps(self.description, ensure_ascii=False)}",
        ]
        if self.license:
            frontmatter.append(f"license: {json.dumps(self.license, ensure_ascii=False)}")
        if self.compatibility:
            frontmatter.append(
                f"compatibility: {json.dumps(self.compatibility, ensure_ascii=False)}"
            )
        if self.metadata:
            frontmatter.append("metadata:")
            frontmatter.extend(
                f"  {json.dumps(key, ensure_ascii=False)}: "
                f"{json.dumps(value, ensure_ascii=False)}"
                for key, value in self.metadata.items()
            )
        body = "\n".join([*frontmatter, "---", "", self.instructions, ""])
        fd, temporary = tempfile.mkstemp(prefix=".skill-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
            os.replace(temporary, target)
        except BaseException:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
            raise
        return target


__all__ = ["Skill"]
