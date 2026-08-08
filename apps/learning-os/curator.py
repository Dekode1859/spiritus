"""
Curator — the single serialized writer of Lexicon's canonical knowledge.

Indexers are append-only per-source writers (one manifest + one note page
each). The Curator is the *only* thing that reads all manifests and writes the
shared, canonical layer:

- ``wiki/.lexicon/entities.json`` — the canonical entity registry
- ``wiki/.lexicon/relations.json`` — relations between entities, each with a
  grounded, natural-language reason
- ``wiki/entities/<type>/<slug>.md`` — one durable page per canonical entity
- ``wiki/indexes/entities.md`` — a self-sufficient metadata index: enough
  about every entity (description + per-source mentions) to answer questions
  from the index alone, without opening every entity page

Everything semantic is the Indexer/Enricher's judgement call, never ours:
which extracted things are real, reusable concepts (``scope == "general"``)
versus a document's own internal jargon (``scope == "local"``); what an
entity's *type* is (a free label, not a fixed enum we maintain); and *why* two
entities relate (a one-sentence, evidence-backed explanation — never a coded
relation type with no reason attached). The Curator only promotes ``general``
entities to canonical pages, and only records relations the Indexer actually
explained between two such entities — so the graph reflects understanding,
not "these words appeared in the same file."

Merging is otherwise fully deterministic (no LLM): entities merge by
normalized *name* alone — never partitioned by type, since the same real
thing can get a slightly different type label from different indexing runs.
A canonical ``type`` is chosen by majority vote across every mention. Entity
pages are derived artifacts, regenerated from the registry every pass, so
they never drift from the evidence. External enrichment (a later LLM stage)
lives in each entity's ``enrichment`` object in the registry and is merged
into the page, so regeneration preserves it.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from source_pipeline import slugify

_EVIDENCE_CAP = 8
_RELATION_CAP = 30
_DEFAULT_TYPE = "topic"


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm_type(value: str) -> str:
    # No fixed enum: whatever label the Indexer chose, lightly normalized so
    # "Library" / "library" / " library " merge into one folder/bucket.
    candidate = str(value or "").strip().lower()
    return candidate or _DEFAULT_TYPE


def _norm_scope(value: str) -> str:
    # Default to "general" when unset so pre-scope manifests still promote.
    return "local" if str(value or "").strip().lower() == "local" else "general"


def _entity_id(name: str) -> str:
    return slugify(name, fallback="entity")


class Curator:
    def __init__(self, workspace_root: Path):
        self._workspace = Path(workspace_root)
        self._wiki = self._workspace / "wiki"
        self._lexicon = self._wiki / ".lexicon"
        self._sources_dir = self._lexicon / "sources"
        self._entities_dir = self._wiki / "entities"
        self._indexes_dir = self._wiki / "indexes"

    # ── Public API ───────────────────────────────────────────────────────────

    def curate(self) -> dict:
        """Rebuild the canonical registry + entity pages from all manifests."""
        manifests = self._load_manifests()
        previous = self._load_registry()
        registry, relations = self._merge(manifests, previous)

        self._write_registry(registry)
        self._write_relations(relations)
        pages = self._write_entity_pages(registry, relations)
        self._write_entity_index(registry, relations)

        enriched = sum(1 for e in registry.values() if (e.get("enrichment") or {}).get("description"))
        by_type: dict[str, int] = defaultdict(int)
        for entity in registry.values():
            by_type[entity["type"]] += 1
        return {
            "ok": True,
            "entities": len(registry),
            "relations": len(relations),
            "pages_written": pages,
            "enriched": enriched,
            "by_type": dict(by_type),
        }

    def impact_of_source(self, source_id: str) -> dict:
        """What removing one source's manifest would do to the canonical layer.

        Pure and read-only: reruns the same deterministic ``_merge`` with and
        without that source's manifest and diffs the two results. No disk
        writes happen here — this only powers a delete confirmation prompt.
        """
        manifests = self._load_manifests()
        if not any(m.get("source_id") == source_id for m in manifests):
            return {"ok": True, "entities_removed": [], "entities_affected": [], "relations_removed": 0}

        without = [m for m in manifests if m.get("source_id") != source_id]
        before, before_relations = self._merge(manifests, {})
        after, after_relations = self._merge(without, {})

        removed = [e for eid, e in before.items() if eid not in after]
        affected = [
            e for eid, e in before.items()
            if eid in after and any(s.get("source_id") == source_id for s in e["sources"])
        ]
        before_pairs = {(r["from"], r["to"]) for r in before_relations}
        after_pairs = {(r["from"], r["to"]) for r in after_relations}

        def brief(entities):
            return sorted(
                ({"name": e["name"], "type": e["type"]} for e in entities),
                key=lambda e: e["name"].lower(),
            )

        return {
            "ok": True,
            "entities_removed": brief(removed),
            "entities_affected": brief(affected),
            "relations_removed": len(before_pairs - after_pairs),
        }

    def registry(self) -> dict:
        registry = self._load_registry()
        by_type: dict[str, int] = defaultdict(int)
        enriched = 0
        entities = []
        for entity in sorted(registry.values(), key=lambda e: (e["type"], e["name"].lower())):
            by_type[entity["type"]] += 1
            is_enriched = bool((entity.get("enrichment") or {}).get("description"))
            enriched += 1 if is_enriched else 0
            entities.append({
                "id": entity["id"],
                "name": entity["name"],
                "type": entity["type"],
                "aliases": entity.get("aliases", []),
                "page": entity.get("page", ""),
                "source_count": len(entity.get("sources", [])),
                "enriched": is_enriched,
            })
        return {
            "ok": True, "entities": entities, "by_type": dict(by_type),
            "total": len(entities), "enriched": enriched,
        }

    # ── Merge ────────────────────────────────────────────────────────────────

    def _merge(self, manifests: list[dict], previous: dict) -> tuple[dict, list[dict]]:
        registry: dict[str, dict] = {}
        type_votes: dict[str, Counter] = defaultdict(Counter)

        # ── Pass 1: canonical entities (general scope only) ──────────────────
        for manifest in manifests:
            source_id = manifest.get("source_id", "")
            title = manifest.get("title", source_id)
            note_page = manifest.get("note_page", "")

            for candidate in manifest.get("entities", []):
                name = str(candidate.get("name", "")).strip()
                if not name or _norm_scope(candidate.get("scope")) != "general":
                    continue                       # local jargon never gets a page
                eid = _entity_id(name)
                type_votes[eid][_norm_type(candidate.get("type"))] += 1

                entity = registry.get(eid)
                if not entity:
                    entity = {
                        "id": eid,
                        "name": name,               # first-seen casing wins
                        "type": "",                 # filled after all votes are in
                        "aliases": set(),
                        "sources": {},               # source_id -> mention record
                        "enrichment": (previous.get(eid, {}) or {}).get("enrichment", {}) or {},
                    }
                    registry[eid] = entity

                for alias in candidate.get("aliases", []) or []:
                    alias = str(alias).strip()
                    if alias and slugify(alias) != slugify(name):
                        entity["aliases"].add(alias)

                # One entry per source: a synthesized one-sentence account of
                # how *this* source specifically mentions/uses the entity —
                # this is what powers the page's "Sources" section, one bullet
                # per source, not a single blended description.
                entity["sources"].setdefault(source_id, {
                    "source_id": source_id,
                    "title": title,
                    "note_page": note_page,
                    "summary": str(candidate.get("evidence", "")).strip(),
                })

        # Canonical type = majority vote across every mention of this entity;
        # ties break toward whichever type was recorded first.
        for eid, entity in registry.items():
            entity["type"] = type_votes[eid].most_common(1)[0][0]
            entity["page"] = f"entities/{entity['type']}/{slugify(entity['name'], fallback='entity')}.md"

        # Name/alias → id lookup, so relations declared by name resolve to the
        # canonical entity (and relations touching un-promoted local terms drop).
        name_to_id: dict[str, str] = {}
        for entity in registry.values():
            name_to_id.setdefault(slugify(entity["name"], fallback="entity"), entity["id"])
            for alias in entity["aliases"]:
                name_to_id.setdefault(slugify(alias, fallback="entity"), entity["id"])

        # ── Pass 2: relations, each carrying a grounded, one-sentence reason ──
        # Keyed by the directed (from, to) pair only — no coded relation type.
        # A relation with no usable explanation is not worth recording at all.
        rel_map: dict[tuple[str, str], dict] = {}
        for manifest in manifests:
            source_id = manifest.get("source_id", "")
            for rel in manifest.get("relations", []):
                explanation = str(rel.get("explanation", "")).strip()
                if not explanation:
                    continue
                fid = name_to_id.get(slugify(str(rel.get("from", "")), fallback="entity"))
                tid = name_to_id.get(slugify(str(rel.get("to", "")), fallback="entity"))
                if not fid or not tid or fid == tid:
                    continue
                key = (fid, tid)
                entry = rel_map.setdefault(key, {"explanation": explanation, "sources": set()})
                entry["sources"].add(source_id)
                if not entry["explanation"]:
                    entry["explanation"] = explanation

        # Freeze sets → sorted lists for stable, serializable output.
        for entity in registry.values():
            entity["aliases"] = sorted(entity["aliases"])
            entity["sources"] = list(entity["sources"].values())

        relations = [
            {"from": f, "to": t, "explanation": entry["explanation"], "sources": sorted(entry["sources"])}
            for (f, t), entry in sorted(rel_map.items())
        ]
        return registry, relations

    # ── Entity pages ─────────────────────────────────────────────────────────

    def _write_entity_pages(self, registry: dict, relations: list[dict]) -> int:
        # Rebuild the entities/ tree from scratch so deleted entities (and
        # entities whose canonical type/folder shifted) don't leave stragglers.
        if self._entities_dir.exists():
            for path in self._entities_dir.rglob("*.md"):
                try:
                    path.unlink()
                except OSError:
                    pass

        by_id = {e["id"]: e for e in registry.values()}
        rels_out: dict[str, list[dict]] = defaultdict(list)
        rels_in: dict[str, list[dict]] = defaultdict(list)
        for rel in relations:
            if rel["from"] in by_id and rel["to"] in by_id:
                rels_out[rel["from"]].append(rel)
                rels_in[rel["to"]].append(rel)

        written = 0
        for entity in registry.values():
            page_path = self._wiki / entity["page"]
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(
                self._render_entity_page(entity, by_id, rels_out, rels_in), encoding="utf-8")
            written += 1
        return written

    def _render_entity_page(self, entity: dict, by_id: dict,
                            rels_out: dict, rels_in: dict) -> str:
        name = entity["name"]
        enrichment = entity.get("enrichment") or {}
        depth = entity["page"].count("/")          # entities/<type>/<slug>.md → 2
        up = "../" * depth

        # The description is the very first thing on the page — a general,
        # internet-common definition, not a source snippet. Until an entity
        # has been enriched, say so plainly rather than faking a description
        # out of one source's evidence.
        lines = [f"# {name}", ""]
        if enrichment.get("description"):
            lines.append(enrichment["description"].strip())
        else:
            lines.append("_Not enriched yet — no general description available._")
        lines.append("")
        lines.append(f"*{entity['type']}*" + (f" · also known as {', '.join(entity['aliases'])}"
                     if entity["aliases"] else ""))

        # Sources — one bullet per source, each a short account of how *that*
        # source specifically mentions or uses the entity.
        lines += ["", "## Sources"]
        for src in entity["sources"][:_EVIDENCE_CAP]:
            note = src.get("note_page", "")
            label = src.get("title") or src.get("source_id")
            link = f"[{label}]({up}{note})" if note else label
            summary = f" — {src['summary']}" if src.get("summary") else ""
            lines.append(f"- {link}{summary}")

        # Relationships — every line carries the reason, not just a category.
        rel_lines: list[str] = []
        for rel in rels_out.get(entity["id"], [])[:_RELATION_CAP]:
            target = by_id.get(rel["to"])
            if target:
                rel_lines.append(f"- [[{target['name']}]] — {rel['explanation']}")
        for rel in rels_in.get(entity["id"], [])[:_RELATION_CAP]:
            source = by_id.get(rel["from"])
            if source:
                rel_lines.append(f"- [[{source['name']}]] — {rel['explanation']}")
        if rel_lines:
            lines += ["", "## Relationships", *rel_lines]

        # External sources — real reference material the enrichment pass drew
        # on, so the reader can go verify or read further, clearly labeled as
        # the model's own recalled knowledge rather than something it browsed
        # live (Lexicon's agents aren't wired to a live search tool today).
        external = enrichment.get("external_sources") or []
        if external:
            lines += ["", "## External Sources",
                     "_Recalled from the model's own knowledge, not fetched live — worth verifying:_", ""]
            for item in external:
                url = str(item.get("url", "")).strip()
                label = str(item.get("label", "")).strip() or url
                if url:
                    lines.append(f"- [{label}]({url})")
        lines.append("")
        return "\n".join(lines)

    def _write_entity_index(self, registry: dict, relations: list[dict]) -> None:
        """A metadata layer complete enough to answer questions about an
        entity — where it came from, one line per source — without opening
        every entity page. Meant for both humans skimming and a future
        chat feature reading this one file for grounding."""
        self._indexes_dir.mkdir(parents=True, exist_ok=True)
        index_path = self._indexes_dir / "entities.md"
        if not registry:
            index_path.unlink(missing_ok=True)
            return

        rel_count: dict[str, int] = defaultdict(int)
        for rel in relations:
            rel_count[rel["from"]] += 1
            rel_count[rel["to"]] += 1

        by_type: dict[str, list[dict]] = defaultdict(list)
        for entity in registry.values():
            by_type[entity["type"]].append(entity)

        lines = ["# Entity Index", "",
                 f"{len(registry)} entities across {len(by_type)} types, {len(relations)} relationships.", ""]
        for entity_type in sorted(by_type):
            entities = sorted(by_type[entity_type], key=lambda e: e["name"].lower())
            lines.append(f"## {entity_type.capitalize()} ({len(entities)})")
            for entity in entities:
                enrichment = entity.get("enrichment") or {}
                description = (enrichment.get("description") or "").strip()
                lines.append(f"### [[{entity['name']}]]")
                lines.append(description if description else "_Not enriched yet._")
                for src in entity["sources"]:
                    label = src.get("title") or src.get("source_id")
                    note = src.get("note_page", "")
                    link = f"[{label}]({note})" if note else label
                    summary = f" — {src['summary']}" if src.get("summary") else ""
                    lines.append(f"- {link}{summary}")
                if rel_count.get(entity["id"]):
                    lines.append(f"- _{rel_count[entity['id']]} relationship"
                                 f"{'' if rel_count[entity['id']] == 1 else 's'} — see the entity page_")
                lines.append("")
        index_path.write_text("\n".join(lines), encoding="utf-8")

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_manifests(self) -> list[dict]:
        if not self._sources_dir.is_dir():
            return []
        manifests = []
        for path in sorted(self._sources_dir.glob("*.json")):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return manifests

    def _load_registry(self) -> dict:
        path = self._lexicon / "entities.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {e["id"]: e for e in data.get("entities", [])}
        except (OSError, json.JSONDecodeError, KeyError):
            return {}

    def _write_registry(self, registry: dict) -> None:
        self._lexicon.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _iso_now(),
            "entities": [registry[k] for k in sorted(registry)],
        }
        (self._lexicon / "entities.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")

    def _write_relations(self, relations: list[dict]) -> None:
        self._lexicon.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": _iso_now(), "relations": relations}
        (self._lexicon / "relations.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
