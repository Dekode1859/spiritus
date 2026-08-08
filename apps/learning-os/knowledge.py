"""
Knowledge-build pipeline (app-local).

Two pieces:

- ``AgentRunner`` drives one OpenCode agent to completion, Python-side. It
  mirrors the proven jobsearch ``runAgentToFile`` pattern: open a session, POST
  a message (which blocks until the agent finishes), then read the file the
  agent was told to write. No SSE/event watching — the message call already
  blocks for the full run.

- ``JobManager`` owns a single background worker thread that turns processed
  sources into knowledge: for each source it runs the Indexer agent, which
  writes a per-source manifest (machine-readable) plus a source-note wiki page
  (human-readable, with [[wikilinks]] that the wiki indexer turns into
  backlinks and graph edges). Job state is persisted under
  ``wiki/.lexicon/jobs/`` so progress survives an app restart.

Invariants:
- Indexers are append-only *per-source* writers (manifest + note page only,
  named by source_id so two similarly-titled sources never collide).
- The shared canonical registries (entities/relations) and every entity page
  are written by exactly one serialized Curator pass, always run on this same
  worker thread — never by parallel indexers, never concurrently with a delete.
- Deleting a source is symmetric with indexing it: remove its manifest, then
  re-run the Curator. Because the Curator fully rebuilds the registry from
  whatever manifests exist on disk (it never patches incrementally), entities
  that only existed because of the deleted source vanish on their own — no
  separate dependency graph to maintain.
- The agent under ``run_to_file`` always writes to a throwaway per-job scratch
  path, never straight to the durable manifest/note locations. ``run_to_file``
  unconditionally clears whatever path it's given before each run (so a stale
  leftover can't be mistaken for fresh output) — if that path were the durable
  manifest itself, a *failed* reprocess-triggered re-index would destroy the
  last known-good manifest before the new one was ever confirmed, and the next
  unrelated Curator pass would then silently drop that source's entities.
  Routing through scratch and only renaming into place after validation means
  a failed run always leaves prior good state untouched.
"""
from __future__ import annotations

import hashlib
import json
import queue
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import requests
from curator import Curator


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AgentRunner:
    """Run one OpenCode agent to completion and return the file it wrote."""

    def __init__(self, server, workspace_root: Path):
        self._server = server              # exposes ``.port``
        self._workspace = Path(workspace_root)

    def run_to_file(self, agent_id: str, prompt: str, out_rel_path: str, timeout: int = 300) -> str:
        port = getattr(self._server, "port", None)
        if not port:
            raise RuntimeError("OpenCode engine is not running")

        base = f"http://127.0.0.1:{port}"
        out_path = self._workspace / out_rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if out_path.exists():
                out_path.unlink()          # clear any stale output before the run
        except OSError:
            pass

        session = requests.post(f"{base}/session", json={}, timeout=30)
        session.raise_for_status()
        session_id = session.json()["id"]

        full_prompt = f"{prompt}\n\nOUTPUT_FILE: workspace/{out_rel_path}"
        message = requests.post(
            f"{base}/session/{session_id}/message",
            json={"agent": agent_id, "parts": [{"type": "text", "text": full_prompt}]},
            timeout=timeout,
        )
        message.raise_for_status()

        if not out_path.exists():
            raise RuntimeError(f"Agent '{agent_id}' did not write {out_rel_path}")
        return out_path.read_text(encoding="utf-8")


INDEXER_PROMPT = """You are the Indexer for Lexicon.ai. Turn ONE processed source \
into durable knowledge. Do not modify anything under raw/ or processed/.

Source id: {source_id}
Title: {title}
Processed document (read it): processed/{source_id}/document.md

The most important part of your job is judgement about WHAT is worth recording
and HOW two things actually relate — not extracting every proper noun you see.

SCOPE — tag every entity as exactly one of:
- "general": a real, widely-known, reusable thing that would have its own page
  in an encyclopedia or its own docs site — a library (Pydantic), a language
  (Python), a genuine concept (data validation, dependency injection), a
  standard (JSON Schema), a named person, a well-known tool or framework.
  These deserve a canonical cross-source page.
- "local": a name that only means something INSIDE this document — a component,
  step, module, or role this author invented ("validator node", "adapter
  compiled spec", "the ingest worker"). Real to this project, but NOT a
  general concept. Do NOT promote these to canonical pages.

Only "general" entities become canonical pages and only they may appear in
relations. When unsure, prefer "local" — a smaller, cleaner set of real
concepts beats a noisy pile of project jargon.

TYPE — a short, free label for what kind of thing this is (e.g. "library",
"programming language", "protocol", "algorithm", "dataset", "person",
"standard"). Pick whatever word actually fits best; do not force it into a
fixed category that doesn't apply — new kinds of things will keep showing up
and you should describe them plainly rather than jam them into the nearest
existing label.

RELATIONS — capture a relationship only when you can explain it in one plain
sentence, grounded in what the document actually says, between two "general"
entities. The explanation IS the relation — there is no separate category
label. Write it like you're telling someone how these two things actually
connect: "Pydantic validates the JSON payloads before FastAPI passes them to
the handler", not "Pydantic relates to FastAPI". Never link two things just
because they happen to appear in the same document — if you can't state a real
reason, don't record the relation.

EVIDENCE — for both entities and relations, write a short, synthesized
one-sentence account of what the document actually says, in your own words —
not a verbatim quote fragment. This becomes the "how it appears in this
source" bullet a reader sees, so make it stand on its own.

Do exactly two things:

1. Write a concise source-note page to `workspace/wiki/{note_page}` (markdown).
   - One or two short paragraphs summarizing what this source is about.
   - Wikilink ONLY "general" entities inline as [[Entity Name]], using natural
     canonical names so the same entity links consistently across sources.
   - Refer to "local" names in plain prose (no [[ ]]).
   - End with a line: `Source: {source_id}`.

2. Write the manifest JSON to the OUTPUT_FILE with these keys:
   - "source_id": "{source_id}"
   - "title": the source title
   - "summary": one-sentence summary
   - "entities": array of {{"name","type","scope","aliases","evidence"}} where
     type is a free label (see TYPE above), scope is general|local, aliases is
     a (possibly empty) array, evidence is the one-sentence account above.
   - "relations": array of {{"from","to","explanation"}} where from/to are
     names of "general" entities you listed above and explanation is the
     one-sentence, grounded reason above. Omit any relation you can't explain.
   - "note_page": "{note_page}"

Keep every claim grounded in the processed document."""


ENRICHER_PROMPT = """You are the Enricher for Lexicon.ai. Explain ONE entity \
using your own general knowledge of the world — NOT limited to the user's
documents. This is what lets the wiki actually understand what things are,
instead of only parroting the sources. This runs automatically the first time
an entity is seen — the user never has to ask for it.

Entity name: {name}
Recorded type: {entity_type}

How it shows up in the user's imported sources:
{evidence}

Write the manifest JSON to the OUTPUT_FILE with exactly these keys:
- "description": 2-4 sentences defining what {name} genuinely is — how it's
  actually described online and by the people who built or use it, what it's
  typically used for, and how it relates to the ideas in the evidence above.
  This is the very first thing a reader of the entity's page sees, so make it
  a real, authoritative account, not hedge-y or vague.
- "external_sources": array of {{"label","url"}} — concrete, real reference
  material you associate with {name} from your own knowledge (official docs,
  the project's own site, a standards body, Wikipedia) that a curious reader
  could open to learn more. This is recalled from what you know, not a live
  search, so only include URLs you are genuinely confident are real and
  correct — it is fine to return an empty array rather than guess a URL.
- "confidence": "high" if {name} is a well-known real thing you can describe
  accurately; "low" if it looks like a project-specific or made-up term you do
  not actually recognize.

Rules:
- If confidence is "low", say plainly in "description" that this appears to be a
  project-specific term, do NOT invent facts about it, and leave
  "external_sources" empty.
- Never contradict the evidence; add real-world context around it.
- Do not read or write any file other than the OUTPUT_FILE."""


class JobManager:
    """Serial background worker: processed source -> Indexer -> manifest + note page."""

    ACTIVE = ("queued", "running")

    def __init__(self, workspace_root: Path, runner, pipeline, *,
                 indexer_agent: str = "indexer", enricher_agent: str = "enricher",
                 enrich_on_index: bool = True, reconcile_on_start: bool = True,
                 autostart: bool = True):
        self._workspace = Path(workspace_root)
        self._runner = runner
        self._pipeline = pipeline
        self._indexer_agent = indexer_agent
        self._enricher_agent = enricher_agent
        self._enrich_on_index = enrich_on_index
        self._curator = Curator(self._workspace)
        # Serializes every write to the shared canonical layer (curate + enrich)
        # so a manual curate/enrich over HTTP can't overlap the worker's own.
        self._curate_lock = threading.Lock()

        self._lexicon = self._workspace / "wiki" / ".lexicon"
        self._jobs_dir = self._lexicon / "jobs"
        self._sources_dir = self._lexicon / "sources"
        self._scratch_root = self._lexicon / ".tmp"
        for folder in (self._jobs_dir, self._sources_dir, self._workspace / "wiki" / "sources"):
            folder.mkdir(parents=True, exist_ok=True)
        # Any scratch dirs left behind by a job interrupted by a crash are
        # harmless leftovers (the job itself gets re-queued below) — sweep
        # them so they don't accumulate across restarts.
        shutil.rmtree(self._scratch_root, ignore_errors=True)
        self._scratch_root.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        # source_ids whose deletion was requested while a job for them was
        # already running; the job discards its output instead of completing.
        self._cancelling: set[str] = set()
        self._load_and_requeue()

        # Self-heal on boot: a source deleted while the app was closed (or a
        # note left by an older indexing scheme) leaves orphans the running app
        # never got to clean up. Best-effort — never block startup over it.
        if reconcile_on_start:
            try:
                self.reconcile()
            except Exception:
                pass

        self._worker = threading.Thread(target=self._run_loop, name="lexicon-indexer", daemon=True)
        if autostart:
            self._worker.start()

    # ── Public API ───────────────────────────────────────────────────────────

    def enqueue(self, source_id: str, source_title: str = "") -> str:
        with self._lock:
            for job in self._jobs.values():
                if job["source_id"] == source_id and job["state"] in self.ACTIVE:
                    return job["id"]              # collapse duplicate active jobs
            job = {
                "id": f"job-{uuid4().hex[:12]}",
                "source_id": source_id,
                "source_title": source_title or source_id,
                "state": "queued",
                "created_at": _iso_now(),
                "started_at": "",
                "finished_at": "",
                "error": "",
                "outputs": [],
                "processed_hash": "",
            }
            self._jobs[job["id"]] = job
            self._persist(job)
        self._queue.put(job["id"])
        return job["id"]

    def rebuild_all(self) -> dict:
        count = 0
        for summary in self._processed_sources():
            self.enqueue(summary["id"], summary.get("title", ""))
            count += 1
        return {"ok": True, "queued": count}

    def rebuild_source(self, source_id: str) -> dict:
        summary = next((s for s in self._processed_sources() if s["id"] == source_id), None)
        if not summary:
            return {"ok": False, "error": f"No processed source: {source_id}"}
        return {"ok": True, "job_id": self.enqueue(source_id, summary.get("title", ""))}

    def retry_failed(self) -> dict:
        with self._lock:
            failed = [j for j in self._jobs.values() if j["state"] == "failed"]
        for job in failed:
            self.enqueue(job["source_id"], job["source_title"])
        return {"ok": True, "queued": len(failed)}

    def curate(self) -> dict:
        """Run the Curator pass on demand (also runs after every indexing job)."""
        with self._curate_lock:
            return self._curator.curate()

    def enrich(self, *, only_missing: bool = True, limit: int | None = None) -> dict:
        """Fill each general entity's description + external context with an
        LLM's general-world knowledge. Serialized with curation so the registry
        has a single writer; safe to trigger manually while the worker runs."""
        with self._curate_lock:
            return self._enrich_locked(only_missing=only_missing, limit=limit)

    def entities(self) -> dict:
        return self._curator.registry()

    def reconcile(self) -> dict:
        """Purge derived knowledge whose source no longer exists, then re-curate.

        The in-app Delete button cascades correctly, but a source can also vanish
        another way: the raw files get deleted outside the app, or an older
        indexing scheme left a note behind under a different name. This makes the
        wiki self-heal — any manifest whose source is gone, and any note page not
        backed by a surviving manifest, is removed, and entities/relations are
        rebuilt from what's left."""
        with self._curate_lock:
            return self._reconcile_locked()

    def _reconcile_locked(self) -> dict:
        live_ids = {s["id"] for s in self._pipeline.list_sources()}
        valid_notes: set[str] = set()
        removed_manifests = 0

        # 1. Drop manifests whose source no longer exists; collect the note
        #    pages the survivors legitimately own.
        for path in sorted(self._sources_dir.glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                removed_manifests += 1
                continue
            source_id = manifest.get("source_id") or path.stem
            if source_id not in live_ids:
                path.unlink(missing_ok=True)
                removed_manifests += 1
                continue
            note = str(manifest.get("note_page") or "").lstrip("/")
            if note:
                valid_notes.add(note)

        # 2. Delete orphan note pages — any wiki/sources/*.md not owned by a
        #    surviving manifest (stale renames, or a deleted source's leftover).
        removed_notes = 0
        notes_dir = self._workspace / "wiki" / "sources"
        if notes_dir.is_dir():
            for note_path in notes_dir.glob("*.md"):
                if f"sources/{note_path.name}" not in valid_notes:
                    note_path.unlink(missing_ok=True)
                    removed_notes += 1

        # 3. Rebuild entities / relations / entity pages from what survives.
        curated = self._curator.curate()
        return {
            "ok": True,
            "removed_manifests": removed_manifests,
            "removed_notes": removed_notes,
            "entities": curated["entities"],
            "relations": curated["relations"],
        }

    def preview_delete(self, source_id: str) -> dict:
        """What deleting this source would do, for a confirmation prompt."""
        source_id = source_id or ""
        summary = next((s for s in self._pipeline.list_sources() if s["id"] == source_id), None)
        if not summary:
            return {"ok": False, "error": f"Unknown source: {source_id}"}

        manifest = self._read_manifest(source_id)
        impact = self._curator.impact_of_source(source_id)
        if not impact["ok"]:
            return impact

        with self._lock:
            active_job = any(j["source_id"] == source_id and j["state"] in self.ACTIVE
                              for j in self._jobs.values())
        return {
            "ok": True,
            "source_id": source_id,
            "title": summary.get("title", source_id),
            "was_indexed": bool(manifest),
            "note_page": manifest.get("note_page", ""),
            "active_job": active_job,
            "entities_removed": impact["entities_removed"],
            "entities_affected": impact["entities_affected"],
            "relations_removed": impact["relations_removed"],
        }

    def forget_source(self, source_id: str) -> dict:
        """Cascade-delete one source: raw + processed files, its manifest and
        note page, its job history, and its contribution to every entity and
        relation — recomputed by re-running the Curator over what remains."""
        source_id = source_id or ""
        with self._lock:
            for job in list(self._jobs.values()):
                if job["source_id"] != source_id:
                    continue
                if job["state"] == "running":
                    self._cancelling.add(source_id)   # cleaned up when the run returns
                    continue                          # its record is removed once it stops
                self._jobs.pop(job["id"], None)
                try:
                    (self._jobs_dir / f"{job['id']}.json").unlink()
                except OSError:
                    pass

        manifest = self._read_manifest(source_id)
        note_deleted = False
        if manifest.get("note_page"):
            note_path = self._workspace / "wiki" / manifest["note_page"]
            if note_path.exists():
                note_path.unlink()
                note_deleted = True
        manifest_path = self._sources_dir / f"{source_id}.json"
        manifest_deleted = manifest_path.exists()
        if manifest_deleted:
            manifest_path.unlink()

        pipeline_result = self._pipeline.delete_source(source_id)
        curated = self._curator.curate()
        return {
            "ok": True,
            "source_id": source_id,
            "files_deleted": pipeline_result.get("ok", False),
            "manifest_deleted": manifest_deleted,
            "note_deleted": note_deleted,
            "entities": curated["entities"],
            "relations": curated["relations"],
        }

    def status(self) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())

        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
        for job in jobs:
            counts[job["state"]] = counts.get(job["state"], 0) + 1

        indexed = stale = unindexed = entities = 0
        for summary in self._processed_sources():
            manifest = self._read_manifest(summary["id"])
            if not manifest:
                unindexed += 1
                continue
            entities += len(manifest.get("entities", []))
            if manifest.get("processed_hash") == self._processed_hash(summary["id"]):
                indexed += 1
            else:
                stale += 1

        registry = self._curator.registry()

        recent = sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)[:8]
        recent_view = [
            {k: job.get(k) for k in
             ("id", "source_id", "source_title", "state", "error", "created_at", "finished_at")}
            for job in recent
        ]
        return {
            "ok": True,
            "enabled": True,
            "counts": counts,
            "sources": {
                "indexed": indexed,
                "stale": stale,
                "unindexed": unindexed,
                "total": indexed + stale + unindexed,
            },
            "entities": entities,
            "registry": {
                "total": registry.get("total", 0),
                "by_type": registry.get("by_type", {}),
                "enriched": registry.get("enriched", 0),
            },
            "recent": recent_view,
        }

    # ── Worker ───────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            except Exception as exc:          # never let the worker thread die
                self._fail(job_id, str(exc))
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["state"] not in self.ACTIVE:
                return
            job["state"] = "running"
            job["started_at"] = _iso_now()
            job["error"] = ""
            self._persist(job)
            source_id = job["source_id"]
            title = job["source_title"]

        processed = self._workspace / "processed" / source_id / "document.md"
        if not processed.exists():
            raise RuntimeError(f"No processed document for {source_id}")
        processed_hash = self._processed_hash(source_id)

        # Deterministic final destinations, keyed by source_id (never a title
        # slug) so two similarly-titled sources can never collide.
        note_page = f"sources/{source_id}.md"
        manifest_rel = f"wiki/.lexicon/sources/{source_id}.json"

        # The agent writes to a throwaway per-job scratch path — never to the
        # durable locations above — so a run that fails partway (or a reindex
        # that never gets a chance to finish) can't clobber a previously good
        # manifest/note. See the module docstring for why this matters.
        #
        # Two path flavors are in play, matching note_page/manifest_rel above:
        # the prompt's {note_page} slot is wiki-relative (the template itself
        # prepends "workspace/wiki/"), while run_to_file's out_rel_path — and
        # therefore the manifest scratch path — is workspace-relative.
        scratch_dir = f".lexicon/.tmp/{job_id}"                      # wiki-relative
        scratch_note_rel = f"{scratch_dir}/note.md"                  # wiki-relative
        scratch_manifest_rel = f"wiki/{scratch_dir}/manifest.json"   # workspace-relative
        prompt = INDEXER_PROMPT.format(source_id=source_id, title=title, note_page=scratch_note_rel)

        try:
            raw = self._runner.run_to_file(self._indexer_agent, prompt, scratch_manifest_rel)

            # The agent call above blocks for the run's full duration. Check
            # for a concurrent delete *before* trusting anything it wrote — a
            # forget_source() that ran while we were blocked means this source
            # is gone; nothing the agent produced should be committed now,
            # regardless of whether its run looked successful.
            with self._lock:
                if source_id in self._cancelling:
                    self._cancelling.discard(source_id)
                    self._jobs.pop(job_id, None)
                    try:
                        (self._jobs_dir / f"{job_id}.json").unlink()
                    except OSError:
                        pass
                    return

            try:
                manifest = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Indexer manifest was not valid JSON: {exc}") from exc

            scratch_note_abs = self._workspace / "wiki" / scratch_note_rel
            if not scratch_note_abs.exists():
                raise RuntimeError(f"Indexer did not write note page: wiki/{scratch_note_rel}")

            # We own the destination, not the agent: overriding whatever it
            # reported keeps "where does this end up" a deterministic
            # decision, not agent-controlled input.
            manifest["source_id"] = source_id
            manifest["note_page"] = note_page
            manifest["processed_hash"] = processed_hash
            manifest["indexed_at"] = _iso_now()

            # Only now, with a fully validated result in hand, touch durable
            # storage — the last step, so it can't half-happen.
            note_abs = self._workspace / "wiki" / note_page
            note_abs.parent.mkdir(parents=True, exist_ok=True)
            scratch_note_abs.replace(note_abs)
            (self._workspace / manifest_rel).write_text(
                json.dumps(manifest, indent=2), encoding="utf-8")
        finally:
            shutil.rmtree(self._workspace / "wiki" / scratch_dir, ignore_errors=True)

        with self._lock:
            job["state"] = "completed"
            job["finished_at"] = _iso_now()
            job["processed_hash"] = processed_hash
            job["outputs"] = [manifest_rel, f"wiki/{note_page}"]
            job["error"] = ""
            self._persist(job)

        # Fold the new manifest into the canonical layer, then (best-effort)
        # enrich any entity that still lacks a general-knowledge description.
        # Both hold _curate_lock, so the shared registry keeps a single writer.
        self.curate()
        self._maybe_enrich()

    def _maybe_enrich(self) -> None:
        if not self._enrich_on_index:
            return
        try:
            self.enrich(only_missing=True)
        except Exception:
            pass   # enrichment is best-effort; never fail an index over it

    # ── Enrichment ───────────────────────────────────────────────────────────

    def _enrich_locked(self, *, only_missing: bool, limit: int | None) -> dict:
        registry = self._curator._load_registry()    # id -> entity (with enrichment dicts)
        if not registry:
            return {"ok": True, "enriched": 0, "errors": 0, "candidates": 0}

        targets = [
            e for e in registry.values()
            if not only_missing or not (e.get("enrichment") or {}).get("description")
        ]
        targets.sort(key=lambda e: (e["type"], e["name"].lower()))
        if limit is not None:
            targets = targets[:limit]

        enriched = errors = 0
        for entity in targets:
            try:
                data = self._run_enricher(entity)
            except Exception:
                errors += 1
                continue
            description = str(data.get("description", "")).strip()
            if not description:
                errors += 1
                continue
            external_sources = [
                {"label": str(item.get("label", "")).strip(), "url": str(item.get("url", "")).strip()}
                for item in (data.get("external_sources") or [])
                if isinstance(item, dict) and str(item.get("url", "")).strip()
            ]
            entity["enrichment"] = {
                "description": description,
                "external_sources": external_sources,
                "confidence": str(data.get("confidence", "")).strip().lower(),
                "enriched_at": _iso_now(),
            }
            enriched += 1

        if enriched:
            # Persist enrichment, then re-render pages: curate() rebuilds the
            # registry from manifests but carries enrichment over from the file
            # we just wrote (matched by entity id), so pages pick it up.
            self._curator._write_registry(registry)
            self._curator.curate()
        return {"ok": True, "enriched": enriched, "errors": errors, "candidates": len(targets)}

    def _run_enricher(self, entity: dict) -> dict:
        evidence_lines = []
        for src in entity.get("sources", [])[:5]:
            snippet = str(src.get("evidence", "")).strip()
            if snippet:
                evidence_lines.append(f"- ({src.get('title', 'source')}) {snippet}")
        evidence = "\n".join(evidence_lines) or "- (no source evidence recorded)"
        prompt = ENRICHER_PROMPT.format(
            name=entity["name"], entity_type=entity["type"], evidence=evidence)

        scratch_dir = self._scratch_root / f"enrich-{entity['id'].replace('/', '_')}"
        scratch_rel = str((scratch_dir / "out.json").relative_to(self._workspace)).replace("\\", "/")
        try:
            raw = self._runner.run_to_file(self._enricher_agent, prompt, scratch_rel)
            return json.loads(raw)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    def _fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["state"] = "failed"
            job["finished_at"] = _iso_now()
            job["error"] = error[-400:]
            self._persist(job)

    # ── Persistence & helpers ────────────────────────────────────────────────

    def _persist(self, job: dict) -> None:
        (self._jobs_dir / f"{job['id']}.json").write_text(
            json.dumps(job, indent=2), encoding="utf-8")

    def _load_and_requeue(self) -> None:
        for path in sorted(self._jobs_dir.glob("job-*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # A job left "running" means the app died mid-build — re-run it.
            if job.get("state") in self.ACTIVE:
                job["state"] = "queued"
            self._jobs[job["id"]] = job
            if job["state"] == "queued":
                self._queue.put(job["id"])

    def _processed_sources(self) -> list[dict]:
        return [s for s in self._pipeline.list_sources() if s.get("status") == "processed"]

    def _read_manifest(self, source_id: str) -> dict:
        path = self._sources_dir / f"{source_id}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _processed_hash(self, source_id: str) -> str:
        path = self._workspace / "processed" / source_id / "document.md"
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()
