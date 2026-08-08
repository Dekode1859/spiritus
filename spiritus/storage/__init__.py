"""
Core storage layer — generic file primitives only.

Per the runtime spec (§7), Core storage has **no knowledge of folder
semantics**. It exposes read/write/list/delete over a root directory with
path-traversal protection. *Which* folders exist and what they *mean* is an
application concern, supplied at call time — never hardcoded here.
"""
from __future__ import annotations

import time
from pathlib import Path

# File types the workspace surfaces. Generic text formats, not domain types.
TEXT_SUFFIXES = (".md", ".txt")


def _safe(root: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``root``; raise if it escapes.

    Containment is checked on the resolved path hierarchy, not on the string
    prefix: a sibling directory whose name merely starts with the root's name
    (root ``/data/ws`` vs ``/data/ws-evil``) is outside the root and must be
    rejected, even though its path string starts with the root's.
    """
    root = Path(root).resolve()
    resolved = (root / rel).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Path '{rel}' escapes the storage root")
    return resolved


def ensure_dirs(root: Path, names: list[str]) -> Path:
    """Ensure the given subdirectories exist under ``root``.

    ``names`` is supplied by the application; Core does not invent any.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def list_dir(root: Path, subdir: str = "") -> list[dict]:
    """List text files in a subdirectory (non-recursive).

    The root is resolved before use so the relative paths below are computed
    against the same spelling the entries have. Callers hand us whatever they
    were configured with, and a path that merely *resolves* to the root — a
    Windows 8.3 name, a macOS ``/var`` → ``/private/var`` symlink, anything
    containing ``..`` — would otherwise make ``relative_to`` raise.
    """
    root = Path(root).resolve()
    target = _safe(root, subdir) if subdir else root
    if not target.is_dir():
        return []
    out = []
    for f in sorted(target.iterdir()):
        if f.is_file() and f.suffix in TEXT_SUFFIXES:
            st = f.stat()
            out.append({
                "name": f.name,
                "path": str(f.relative_to(root)),
                "size": st.st_size,
                "modified": int(st.st_mtime * 1000),
            })
    return out


def count_dir(root: Path, subdir: str) -> int:
    """Count text files in a subdirectory."""
    target = _safe(Path(root), subdir)
    if not target.is_dir():
        return 0
    return sum(1 for f in target.iterdir()
               if f.is_file() and f.suffix in TEXT_SUFFIXES)


def read(root: Path, rel: str) -> dict:
    p = _safe(Path(root), rel)
    if not p.exists():
        return {"error": f"File not found: {rel}"}
    return {
        "content": p.read_text(encoding="utf-8"),
        "path": rel,
        "modified": int(p.stat().st_mtime * 1000),
    }


def write(root: Path, rel: str, content: str) -> dict:
    p = _safe(Path(root), rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": rel, "modified": int(p.stat().st_mtime * 1000)}


def delete(root: Path, rel: str) -> dict:
    p = _safe(Path(root), rel)
    if not p.exists():
        return {"error": f"File not found: {rel}"}
    p.unlink()
    return {"ok": True, "path": rel}


def timestamped_name(folder: str, title: str = "") -> str:
    """Build a timestamped relative path inside ``folder``.

    ``folder`` is provided by the caller (the app); Core picks no default.
    """
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    slug = title.lower().replace(" ", "-")[:40] if title else "note"
    return f"{folder}/{ts}-{slug}.md"
