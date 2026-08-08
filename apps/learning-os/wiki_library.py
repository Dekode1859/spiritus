"""
Wiki library — deterministic indexing of the ``wiki/`` layer.

The wiki is a folder of markdown pages maintained by the agents (weekly
folders, topic pages, indexes). This module gives the UI a structured view
of it: page metadata, resolved outgoing links ([[wikilinks]] and relative
markdown links), backlinks, and the edge list that drives the graph view.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

# [[Target]], [[Target#Section]], [[Target|Label]]
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
# [label](relative/page.md) — external URLs are ignored by the scheme check below
MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


class WikiLibrary:
    def __init__(self, workspace_root: Path):
        # Resolved once, because _safe() resolves the paths it returns and
        # page() computes them relative to this root. A workspace spelled
        # differently from its resolved form — a Windows 8.3 name, the macOS
        # /var → /private/var symlink, anything with .. in it — otherwise makes
        # every page lookup fail with a confusing "not in the subpath" error.
        self.wiki_root = (Path(workspace_root) / "wiki").resolve()

    # ── Public API ───────────────────────────────────────────────────────────

    def index(self) -> dict:
        """Full wiki index: pages with metadata, resolved links, backlinks,
        plus the deduplicated edge list for the graph view."""
        pages = self._scan_pages()
        lookup = self._build_lookup(pages)

        edges: list[dict] = []
        seen: set[tuple[str, str]] = set()
        backlinks: dict[str, list[str]] = {page["path"]: [] for page in pages}

        for page in pages:
            resolved: list[str] = []
            for target in page.pop("_raw_targets"):
                dest = self._resolve_target(target, page["path"], lookup)
                if not dest or dest == page["path"] or dest in resolved:
                    continue
                resolved.append(dest)
                key = (page["path"], dest)
                if key not in seen:
                    seen.add(key)
                    edges.append({"source": page["path"], "target": dest})
            page["links"] = resolved
            for dest in resolved:
                backlinks.setdefault(dest, []).append(page["path"])

        for page in pages:
            page["backlinks"] = backlinks.get(page["path"], [])

        return {"ok": True, "pages": pages, "edges": edges}

    def page(self, rel_path: str) -> dict:
        """One page with full content plus its index entry (links/backlinks)."""
        try:
            path = self._safe(rel_path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not path.exists() or not path.is_file():
            return {"ok": False, "error": f"Wiki page not found: {rel_path}"}

        content = path.read_text(encoding="utf-8", errors="replace")
        idx = self.index()
        rel = path.relative_to(self.wiki_root).as_posix()
        entry = next((p for p in idx["pages"] if p["path"] == rel), None)
        titles = {p["path"]: p["title"] for p in idx["pages"]}

        def describe(paths: list[str]) -> list[dict]:
            return [{"path": p, "title": titles.get(p, p)} for p in paths]

        return {
            "ok": True,
            "page": {
                **(entry or {"path": rel, "title": path.stem, "links": [], "backlinks": []}),
                "content": content,
                "links": describe(entry["links"]) if entry else [],
                "backlinks": describe(entry["backlinks"]) if entry else [],
            },
        }

    # ── Internals ────────────────────────────────────────────────────────────

    def _safe(self, rel: str) -> Path:
        # Containment is checked on the path hierarchy, not the string prefix:
        # a sibling directory whose name merely starts with the root's name
        # (root .../wiki, target .../wiki-backup) is outside the root even
        # though its path string begins with it. Mirrors spiritus.storage._safe.
        root = self.wiki_root.resolve()
        resolved = (root / str(rel or "")).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Path '{rel}' escapes the wiki root")
        return resolved

    def _scan_pages(self) -> list[dict]:
        if not self.wiki_root.is_dir():
            return []
        pages = []
        for path in sorted(self.wiki_root.rglob("*.md")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.wiki_root).as_posix()
            # Skip machine-only metadata (wiki/.lexicon/) and any hidden folder.
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            heading = HEADING_RE.search(text)
            title = heading.group(1).strip() if heading else path.stem.replace("-", " ").replace("_", " ").strip()
            stat = path.stat()
            pages.append({
                "path": rel,
                "name": path.name,
                "title": title or path.stem,
                "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
                "modified": int(stat.st_mtime * 1000),
                "size": stat.st_size,
                "word_count": len(re.findall(r"\b\w+\b", text)),
                "excerpt": self._excerpt(text),
                "_raw_targets": self._extract_targets(text),
            })
        pages.sort(key=lambda p: p["modified"], reverse=True)
        return pages

    @staticmethod
    def _excerpt(text: str, limit: int = 180) -> str:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"[*_`>\[\]]|\(\S+\)", "", line).strip()
            if line:
                return line[:limit]
        return ""

    @staticmethod
    def _extract_targets(text: str) -> list[str]:
        targets = [m.group(1).strip() for m in WIKILINK_RE.finditer(text)]
        for m in MDLINK_RE.finditer(text):
            href = m.group(1).strip()
            if "://" not in href:
                targets.append(unquote(href))
        return targets

    @staticmethod
    def _build_lookup(pages: list[dict]) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for page in pages:
            stem = Path(page["path"]).stem
            for key in (_slug(stem), _slug(page["title"])):
                if key:
                    lookup.setdefault(key, page["path"])
            lookup.setdefault(page["path"].lower(), page["path"])
        return lookup

    def _resolve_target(self, target: str, from_path: str, lookup: dict[str, str]) -> str:
        target = target.strip()
        if not target:
            return ""
        if target.endswith(".md"):
            # Relative markdown link: resolve against the page's folder (handling
            # ./ and ../ segments), then fall back to root, then to a stem match.
            base = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
            combined = target.lstrip("/") if target.startswith("/") \
                else (f"{base}/{target}" if base else target)
            for candidate in (combined, target.lstrip("/")):
                normalized = posixpath.normpath(candidate.replace("\\", "/")).lower()
                if normalized in lookup:
                    return lookup[normalized]
            return lookup.get(_slug(Path(target).stem), "")
        return lookup.get(_slug(target), "")
