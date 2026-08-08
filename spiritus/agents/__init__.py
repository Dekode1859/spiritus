"""
Agent system (Core side) — generic loading only.

Agents are **not implemented in Core** (runtime spec §8). OpenCode executes the
agent definitions declared in the application's ``opencode.json``. This module
just reads those declarations so the UI can present them (names, labels,
default model). It contains no agent prompts or logic — those are the app's.
"""
from __future__ import annotations

import json
from pathlib import Path


def _titleize(name: str) -> str:
    return " ".join(w.capitalize() for w in name.replace("-", " ").split())


def load_agents(project_root: Path) -> list[dict]:
    """Read agent definitions from the app's opencode.json.

    Returns ``[{name, label, description}]``. If the file is missing/invalid,
    returns an empty list — Core never fabricates agents.
    """
    oc_path = Path(project_root) / "opencode.json"
    try:
        cfg = json.loads(oc_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    agents = cfg.get("agent", {}) or {}
    out = []
    for name, spec in agents.items():
        label = (spec or {}).get("label") or _titleize(name)
        out.append({
            "name": name,
            "label": label,
            "description": (spec or {}).get("description", ""),
        })
    return out


def default_model(project_root: Path) -> str:
    """Return the app's configured default model string, or ''."""
    oc_path = Path(project_root) / "opencode.json"
    try:
        return json.loads(oc_path.read_text(encoding="utf-8")).get("model", "")
    except Exception:
        return ""
