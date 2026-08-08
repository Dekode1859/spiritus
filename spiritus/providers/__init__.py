"""
Provider system — generic LLM provider abstraction.

Configures/lists providers, stores credentials, and switches the default model.
Provider IDs (anthropic, openai, opencode, …) are generic infrastructure, not
domain concepts. Nothing here knows what the app *does*.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

# Curated providers shown prominently in the settings UI. Generic infra list.
FEATURED_PROVIDERS = [
    "opencode",     # OpenCode Zen — always connected, free models
    "anthropic",
    "openai",
    "ollama-cloud",
    "openrouter",
    "requesty",
    "google",
    "deepseek",
    "groq",
]


def _auth_path(opencode_home: Path) -> Path:
    return opencode_home / ".local" / "share" / "opencode" / "auth.json"


def list_providers(port: int | None) -> dict:
    """Return featured + connected providers from OpenCode's /provider endpoint."""
    if not port:
        return {"featured": [], "connected": []}
    try:
        r = requests.get(f"http://127.0.0.1:{port}/provider", timeout=5)
        data = r.json()
    except Exception as e:
        return {"error": str(e), "featured": [], "connected": []}

    all_providers = {p["id"]: p for p in data.get("all", [])}
    connected = data.get("connected", [])

    featured = []
    for pid in FEATURED_PROVIDERS:
        p = all_providers.get(pid)
        if not p:
            continue
        models = list(p.get("models", {}).values())
        featured.append({
            "id": p["id"],
            "name": p["name"],
            "env": p.get("env", []),
            "connected": p["id"] in connected,
            "models": [
                {"id": m["id"], "name": m.get("name", m["id"]), "cost": m.get("cost", {})}
                for m in models[:80]
            ],
        })
    return {"featured": featured, "connected": connected}


def save_key(opencode_home: Path, provider_id: str, api_key: str) -> dict:
    """Write an API key to the project-isolated auth.json."""
    auth_path = _auth_path(opencode_home)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing[provider_id] = {"type": "api", "key": api_key}
    auth_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return {"ok": True}


def remove_key(opencode_home: Path, provider_id: str) -> dict:
    auth_path = _auth_path(opencode_home)
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text(encoding="utf-8"))
            existing.pop(provider_id, None)
            auth_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass
    return {"ok": True}


def set_default_model(project_root: Path, provider_id: str, model_id: str) -> dict:
    """Persist the default model in the app's opencode.json (providerID/modelID)."""
    oc_path = Path(project_root) / "opencode.json"
    config = json.loads(oc_path.read_text(encoding="utf-8"))
    config["model"] = f"{provider_id}/{model_id}"
    oc_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return {"ok": True, "model": config["model"]}
