from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "apps" / "learning-os"
sys.path.insert(0, str(APP_ROOT))

from knowledge import JobManager  # noqa: E402
from source_pipeline import SourcePipeline  # noqa: E402
from wiki_library import WikiLibrary  # noqa: E402


class FakeRunner:
    """Stands in for AgentRunner: writes the same two files a real Indexer would,
    with deterministic content, so the pipeline is testable without OpenCode.

    _process now hands run_to_file a scratch path (wiki/.lexicon/.tmp/<job>/...),
    not the durable manifest location, so — like a real agent reading the
    prompt — this parses source_id and the scratch note-page path out of the
    prompt text rather than out_rel_path's filename.
    """

    def __init__(self, workspace: Path, *, entities=("Alpha", "Beta"),
                 relations=(), scope="general", fail=False):
        self._workspace = Path(workspace)
        self._entities = entities
        self._relations = relations         # iterable of (from, to, explanation)
        self._scope = scope
        self._fail = fail
        self.calls = 0

    def run_to_file(self, agent_id, prompt, out_rel_path, timeout=300):
        self.calls += 1
        if self._fail:
            raise RuntimeError("simulated agent failure")
        out_abs = self._workspace / out_rel_path
        out_abs.parent.mkdir(parents=True, exist_ok=True)

        if agent_id == "enricher":
            name = re.search(r"Entity name: (.+)", prompt).group(1).strip()
            payload = {
                "description": f"{name} is a well-known thing in general, used for its usual purpose.",
                "external_sources": [{"label": f"{name} docs", "url": f"https://example.com/{name.lower()}"}],
                "confidence": "high",
            }
            out_abs.write_text(json.dumps(payload), encoding="utf-8")
            return out_abs.read_text(encoding="utf-8")

        source_id = re.search(r"Source id: (\S+)", prompt).group(1)
        note_rel = re.search(r"workspace/wiki/(\S+)`", prompt).group(1)
        note_abs = self._workspace / "wiki" / note_rel
        note_abs.parent.mkdir(parents=True, exist_ok=True)
        links = " ".join(f"[[{name}]]" for name in self._entities)
        note_abs.write_text(f"# Note {source_id}\n\nMentions {links}.\n\nSource: {source_id}\n",
                            encoding="utf-8")
        manifest = {
            "source_id": source_id,
            "title": f"Note {source_id}",
            "summary": "A test note.",
            "entities": [{"name": n, "type": "concept", "scope": self._scope,
                          "aliases": [], "evidence": f"how {n} is used"}
                         for n in self._entities],
            "relations": [{"from": f, "to": t, "explanation": ex}
                          for (f, t, ex) in self._relations],
            "note_page": note_rel,
        }
        out_abs.write_text(json.dumps(manifest), encoding="utf-8")
        return out_abs.read_text(encoding="utf-8")


class CancellingRunner(FakeRunner):
    """Behaves like FakeRunner but invokes a callback right after writing its
    output files, simulating a concurrent forget_source() call arriving while
    the (blocking) agent call is still in flight."""

    def __init__(self, workspace: Path, on_written, **kwargs):
        super().__init__(workspace, **kwargs)
        self._on_written = on_written

    def run_to_file(self, agent_id, prompt, out_rel_path, timeout=300):
        result = super().run_to_file(agent_id, prompt, out_rel_path, timeout)
        self._on_written()
        return result


class KnowledgePipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # lexicon-indexer is a daemon thread and may still be writing
        self.workspace = Path(self._tmpdir.name) / "workspace"
        self.pipeline = SourcePipeline(self.workspace)
        self.wiki = WikiLibrary(self.workspace)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _add_source(self, name="notes.txt", text="Gradient descent updates parameters.\n"):
        path = Path(self._tmpdir.name) / name
        path.write_text(text, encoding="utf-8")
        return self.pipeline.import_file(path)["source"]

    def _manager(self, runner, *, enrich_on_index=False, reconcile_on_start=False):
        # Auto-enrich and startup reconcile are off by default here so indexing
        # tests exercise just the index→curate path; both have dedicated tests.
        return JobManager(self.workspace, runner, self.pipeline,
                          enrich_on_index=enrich_on_index,
                          reconcile_on_start=reconcile_on_start, autostart=False)

    def _run_one(self, jm, source_id, title=""):
        # Drive one job synchronously, mirroring _run_loop's try/except so a
        # failing job is recorded (not raised) exactly as the worker would.
        job_id = jm.enqueue(source_id, title)
        try:
            jm._process(job_id)
        except Exception as exc:
            jm._fail(job_id, str(exc))
        return jm._jobs[job_id]

    def test_index_job_writes_manifest_and_note_page(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))
        job = self._run_one(jm, source["id"], source["title"])

        self.assertEqual(job["state"], "completed")
        manifest_path = self.workspace / "wiki" / ".lexicon" / "sources" / f"{source['id']}.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_id"], source["id"])
        self.assertTrue(manifest["processed_hash"])
        self.assertIn("indexed_at", manifest)

        note = self.workspace / "wiki" / manifest["note_page"]
        self.assertTrue(note.exists())

    def test_generated_note_becomes_wiki_page_with_backlinks(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Gradient Descent",)))
        self._run_one(jm, source["id"], source["title"])

        index = self.wiki.index()
        paths = {p["path"] for p in index["pages"]}
        note_path = f"sources/{source['id']}.md"
        self.assertIn(note_path, paths)
        # The .lexicon manifest folder must not surface as a wiki page.
        self.assertFalse(any(".lexicon" in p for p in paths))

    def test_status_counts_indexed_and_unindexed(self):
        first = self._add_source("a.txt", "Alpha content.\n")
        self._add_source("b.txt", "Beta content.\n")   # unindexed, for the count
        jm = self._manager(FakeRunner(self.workspace))
        self._run_one(jm, first["id"], first["title"])

        status = jm.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["sources"]["indexed"], 1)
        self.assertEqual(status["sources"]["unindexed"], 1)
        self.assertEqual(status["sources"]["total"], 2)
        self.assertEqual(status["counts"]["completed"], 1)
        self.assertGreaterEqual(status["entities"], 2)

    def test_reprocess_makes_source_stale(self):
        source = self._add_source("a.txt", "Original content.\n")
        jm = self._manager(FakeRunner(self.workspace))
        self._run_one(jm, source["id"], source["title"])
        self.assertEqual(jm.status()["sources"]["indexed"], 1)

        # Rewrite the raw file with new bytes, then reprocess -> processed hash changes.
        raw = Path(self._tmpdir.name) / "a.txt"
        raw.write_text("Completely different content now.\n", encoding="utf-8")
        # Same source id (content hash of file changes -> new id), so import fresh
        # and instead simulate an in-place processed change:
        doc = self.workspace / "processed" / source["id"] / "document.md"
        doc.write_text(doc.read_text(encoding="utf-8") + "\nappended change\n", encoding="utf-8")

        status = jm.status()
        self.assertEqual(status["sources"]["stale"], 1)
        self.assertEqual(status["sources"]["indexed"], 0)

    def test_failed_reindex_does_not_destroy_a_prior_good_manifest(self):
        # Regression: a naive implementation lets run_to_file's "clear stale
        # output before this run" step delete the *durable* manifest before a
        # replacement is confirmed. If the re-run then fails, the source's
        # entities silently vanish from the next unrelated Curator pass — even
        # though nothing about that source actually changed.
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Alpha",)))
        good_job = self._run_one(jm, source["id"], source["title"])
        self.assertEqual(good_job["state"], "completed")
        self.assertEqual({e["name"] for e in jm.entities()["entities"]}, {"Alpha"})

        manifest_path = self.workspace / "wiki" / ".lexicon" / "sources" / f"{source['id']}.json"
        note_path = self.workspace / "wiki" / "sources" / f"{source['id']}.md"
        self.assertTrue(manifest_path.exists())
        self.assertTrue(note_path.exists())

        # A second run for the same source (e.g. after reprocessing) fails.
        jm._runner = FakeRunner(self.workspace, fail=True)
        failed_job = self._run_one(jm, source["id"], source["title"])
        self.assertEqual(failed_job["state"], "failed")

        # The prior good manifest and note must be untouched.
        self.assertTrue(manifest_path.exists())
        self.assertTrue(note_path.exists())
        self.assertEqual({e["name"] for e in jm.entities()["entities"]}, {"Alpha"})

        # And an unrelated Curator pass (e.g. from indexing a different
        # source) must not silently drop this source's contribution.
        jm.curate()
        self.assertEqual({e["name"] for e in jm.entities()["entities"]}, {"Alpha"})

    def test_scratch_dir_is_cleaned_up_after_a_successful_run(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))
        self._run_one(jm, source["id"], source["title"])
        scratch_root = self.workspace / "wiki" / ".lexicon" / ".tmp"
        self.assertEqual(list(scratch_root.iterdir()), [])

    def test_scratch_dir_is_cleaned_up_after_a_failed_run(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, fail=True))
        self._run_one(jm, source["id"], source["title"])
        scratch_root = self.workspace / "wiki" / ".lexicon" / ".tmp"
        self.assertEqual(list(scratch_root.iterdir()), [])

    def test_failed_job_records_error_and_retry_requeues(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, fail=True))
        job = self._run_one(jm, source["id"], source["title"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("simulated agent failure", job["error"])

        result = jm.retry_failed()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(jm.status()["counts"]["queued"], 1)

    def test_duplicate_active_job_is_collapsed(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))
        first = jm.enqueue(source["id"], source["title"])
        second = jm.enqueue(source["id"], source["title"])
        self.assertEqual(first, second)

    def test_state_persists_and_reloads(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))
        self._run_one(jm, source["id"], source["title"])

        # A fresh manager over the same workspace sees the completed job.
        reloaded = self._manager(FakeRunner(self.workspace))
        status = reloaded.status()
        self.assertEqual(status["counts"]["completed"], 1)
        self.assertEqual(status["sources"]["indexed"], 1)

    def test_interrupted_running_job_is_requeued_on_restart(self):
        source = self._add_source()
        jobs_dir = self.workspace / "wiki" / ".lexicon" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / "job-stuck0000000.json").write_text(json.dumps({
            "id": "job-stuck0000000", "source_id": source["id"],
            "source_title": source["title"], "state": "running",
            "created_at": "2026-01-01T00:00:00Z", "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "", "error": "", "outputs": [], "processed_hash": "",
        }), encoding="utf-8")

        reloaded = self._manager(FakeRunner(self.workspace))
        self.assertEqual(reloaded.status()["counts"]["queued"], 1)

    # ── Cascade delete ───────────────────────────────────────────────────────

    def test_note_page_is_keyed_by_source_id_not_title_slug(self):
        # Two sources with the *same title* must never collide on one note file.
        a = self._add_source("a.txt", "Same title content A.\n")
        b = self._add_source("b.txt", "Same title content B.\n")
        jm = self._manager(FakeRunner(self.workspace))
        self._run_one(jm, a["id"], "Duplicate Title")
        self._run_one(jm, b["id"], "Duplicate Title")

        note_a = self.workspace / "wiki" / "sources" / f"{a['id']}.md"
        note_b = self.workspace / "wiki" / "sources" / f"{b['id']}.md"
        self.assertTrue(note_a.exists())
        self.assertTrue(note_b.exists())
        self.assertNotEqual(note_a, note_b)

    def test_forget_source_deletes_raw_processed_manifest_and_note(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))
        self._run_one(jm, source["id"], source["title"])

        manifest_path = self.workspace / "wiki" / ".lexicon" / "sources" / f"{source['id']}.json"
        note_path = self.workspace / "wiki" / "sources" / f"{source['id']}.md"
        self.assertTrue(manifest_path.exists())
        self.assertTrue(note_path.exists())

        result = jm.forget_source(source["id"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["files_deleted"])
        self.assertTrue(result["manifest_deleted"])
        self.assertTrue(result["note_deleted"])
        self.assertFalse((self.workspace / "raw" / source["id"]).exists())
        self.assertFalse((self.workspace / "processed" / source["id"]).exists())
        self.assertFalse(manifest_path.exists())
        self.assertFalse(note_path.exists())

    def test_forget_source_removes_solely_owned_entities_and_keeps_shared_ones(self):
        a = self._add_source("a.txt", "Content A.\n")
        b = self._add_source("b.txt", "Content B.\n")
        jm = self._manager(FakeRunner(self.workspace, entities=("OnlyA", "Shared")))
        self._run_one(jm, a["id"], a["title"])
        jm2_runner = FakeRunner(self.workspace, entities=("Shared",))
        jm._runner = jm2_runner
        self._run_one(jm, b["id"], b["title"])

        registry = jm.entities()
        names = {e["name"] for e in registry["entities"]}
        self.assertEqual(names, {"OnlyA", "Shared"})

        jm.forget_source(a["id"])
        registry = jm.entities()
        names = {e["name"] for e in registry["entities"]}
        self.assertEqual(names, {"Shared"})   # OnlyA vanished, Shared survived

    def test_forget_source_purges_job_history_for_that_source(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, fail=True))
        self._run_one(jm, source["id"], source["title"])   # leaves a failed job
        self.assertEqual(jm.status()["counts"]["failed"], 1)

        jm.forget_source(source["id"])
        status = jm.status()
        self.assertEqual(status["counts"].get("failed", 0), 0)
        jobs_left = list((self.workspace / "wiki" / ".lexicon" / "jobs").glob("job-*.json"))
        self.assertEqual(jobs_left, [])

    def test_forget_source_cancels_a_queued_job(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))
        job_id = jm.enqueue(source["id"], source["title"])   # never processed, still queued

        result = jm.forget_source(source["id"])
        self.assertTrue(result["ok"])
        self.assertNotIn(job_id, jm._jobs)
        self.assertEqual(jm.status()["counts"].get("queued", 0), 0)

    def test_forget_source_discards_output_from_a_job_racing_with_delete(self):
        source = self._add_source()

        def mid_run_delete():
            # Simulate the user clicking Delete while the agent call above is
            # still blocking: forget_source runs concurrently with _process.
            jm.forget_source(source["id"])

        runner = CancellingRunner(self.workspace, mid_run_delete)
        jm = self._manager(runner)
        job_id = jm.enqueue(source["id"], source["title"])
        # Drive it exactly like the real worker loop would (no manual _fail
        # shortcut here — we want the real cancellation branch in _process).
        jm._process(job_id)

        # The job record must be gone, not left dangling in a weird state.
        self.assertNotIn(job_id, jm._jobs)
        # And nothing the agent wrote during the race should have survived.
        manifest_path = self.workspace / "wiki" / ".lexicon" / "sources" / f"{source['id']}.json"
        note_path = self.workspace / "wiki" / "sources" / f"{source['id']}.md"
        self.assertFalse(manifest_path.exists())
        self.assertFalse(note_path.exists())
        # forget_source itself already ran to completion inside the callback.
        self.assertFalse((self.workspace / "raw" / source["id"]).exists())

    def test_preview_delete_reports_impact_before_deleting(self):
        a = self._add_source("a.txt", "Content A.\n")
        b = self._add_source("b.txt", "Content B.\n")
        jm = self._manager(FakeRunner(self.workspace, entities=("OnlyA", "Shared")))
        self._run_one(jm, a["id"], a["title"])
        jm._runner = FakeRunner(self.workspace, entities=("Shared",))
        self._run_one(jm, b["id"], b["title"])

        preview = jm.preview_delete(a["id"])
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["was_indexed"])
        self.assertEqual({e["name"] for e in preview["entities_removed"]}, {"OnlyA"})
        self.assertEqual({e["name"] for e in preview["entities_affected"]}, {"Shared"})
        self.assertFalse(preview["active_job"])

        # Nothing should have been deleted by merely previewing.
        self.assertTrue((self.workspace / "raw" / a["id"]).exists())

    def test_preview_delete_unknown_source_errors(self):
        jm = self._manager(FakeRunner(self.workspace))
        result = jm.preview_delete("src-nope")
        self.assertFalse(result["ok"])

    def test_forget_source_on_never_indexed_source_still_removes_files(self):
        # Import but never run the indexer job at all.
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace))

        result = jm.forget_source(source["id"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["files_deleted"])
        self.assertFalse(result["manifest_deleted"])
        self.assertFalse(result["note_deleted"])
        self.assertFalse((self.workspace / "raw" / source["id"]).exists())

    # ── Enrichment ───────────────────────────────────────────────────────────

    def test_enrich_fills_description_and_external_sources(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Pydantic",)))
        self._run_one(jm, source["id"], source["title"])

        # Before enrichment: no general description on the entity.
        before = jm.entities()["entities"]
        self.assertTrue(before)
        self.assertFalse(before[0]["enriched"])

        result = jm.enrich()
        self.assertTrue(result["ok"])
        self.assertEqual(result["enriched"], 1)

        after = {e["name"]: e for e in jm.entities()["entities"]}
        self.assertTrue(after["Pydantic"]["enriched"])
        page = self.workspace / "wiki" / "entities" / "concept" / "pydantic.md"
        body = page.read_text(encoding="utf-8")
        # Description leads the page, directly under the title.
        self.assertTrue(body.startswith("# Pydantic\n\nPydantic is a well-known thing"))
        self.assertIn("## External Sources", body)
        self.assertIn("[Pydantic docs](https://example.com/pydantic)", body)
        self.assertIn("Recalled from the model's own knowledge, not fetched live", body)

    def test_enrich_only_missing_is_idempotent(self):
        source = self._add_source()
        runner = FakeRunner(self.workspace, entities=("Pydantic",))
        jm = self._manager(runner)
        self._run_one(jm, source["id"], source["title"])

        first = jm.enrich()
        self.assertEqual(first["enriched"], 1)
        calls_after_first = runner.calls
        second = jm.enrich()
        self.assertEqual(second["enriched"], 0)            # nothing left to enrich
        self.assertEqual(second["candidates"], 0)
        self.assertEqual(runner.calls, calls_after_first)  # no extra agent calls

    def test_enrichment_survives_new_index_and_recuration(self):
        a = self._add_source("a.txt", "Content A.\n")
        jm = self._manager(FakeRunner(self.workspace, entities=("Pydantic",)))
        self._run_one(jm, a["id"], a["title"])
        jm.enrich()
        self.assertTrue(jm.entities()["entities"][0]["enriched"])

        # Index a second, unrelated source — the Curator re-runs, but the
        # earlier entity's enrichment must not be wiped.
        b = self._add_source("b.txt", "Content B.\n")
        jm._runner = FakeRunner(self.workspace, entities=("FastAPI",))
        self._run_one(jm, b["id"], b["title"])

        by_name = {e["name"]: e for e in jm.entities()["entities"]}
        self.assertTrue(by_name["Pydantic"]["enriched"])
        self.assertFalse(by_name["FastAPI"]["enriched"])   # newly seen, not yet enriched

    def test_auto_enrich_on_index_when_enabled(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Pydantic",)),
                           enrich_on_index=True)
        self._run_one(jm, source["id"], source["title"])
        self.assertEqual(jm.status()["registry"]["enriched"], 1)

    # ── Reconcile (self-heal deleted / orphaned sources) ─────────────────────

    def test_reconcile_removes_orphan_note_page(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Pydantic",)))
        self._run_one(jm, source["id"], source["title"])

        # Simulate a leftover note from an older indexing scheme (a note file
        # not owned by any manifest — exactly the project-spec.md situation).
        orphan = self.workspace / "wiki" / "sources" / "project-spec.md"
        orphan.write_text("# project-spec\n\nMentions [[Ghost]].\n", encoding="utf-8")
        self.assertIn("sources/project-spec.md",
                      {p["path"] for p in self.wiki.index()["pages"]})

        result = jm.reconcile()
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_notes"], 1)
        self.assertFalse(orphan.exists())
        # The real source's note must survive.
        self.assertTrue((self.workspace / "wiki" / "sources" / f"{source['id']}.md").exists())
        self.assertNotIn("sources/project-spec.md",
                         {p["path"] for p in self.wiki.index()["pages"]})

    def test_reconcile_drops_manifest_and_entities_for_a_vanished_source(self):
        a = self._add_source("a.txt", "Content A.\n")
        b = self._add_source("b.txt", "Content B.\n")
        jm = self._manager(FakeRunner(self.workspace, entities=("OnlyA",)))
        self._run_one(jm, a["id"], a["title"])
        jm._runner = FakeRunner(self.workspace, entities=("OnlyB",))
        self._run_one(jm, b["id"], b["title"])
        self.assertEqual({e["name"] for e in jm.entities()["entities"]}, {"OnlyA", "OnlyB"})

        # Delete source A's raw+processed *outside the app* (no forget_source).
        self.pipeline.delete_source(a["id"])
        # Its manifest, note, and entity are now stale.
        self.assertTrue((self.workspace / "wiki" / ".lexicon" / "sources" / f"{a['id']}.json").exists())

        result = jm.reconcile()
        self.assertEqual(result["removed_manifests"], 1)
        self.assertEqual(result["removed_notes"], 1)
        self.assertFalse((self.workspace / "wiki" / ".lexicon" / "sources" / f"{a['id']}.json").exists())
        self.assertEqual({e["name"] for e in jm.entities()["entities"]}, {"OnlyB"})

    def test_reconcile_leaves_a_healthy_workspace_untouched(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Pydantic",)))
        self._run_one(jm, source["id"], source["title"])
        before = {e["name"] for e in jm.entities()["entities"]}

        result = jm.reconcile()
        self.assertEqual(result["removed_manifests"], 0)
        self.assertEqual(result["removed_notes"], 0)
        self.assertEqual({e["name"] for e in jm.entities()["entities"]}, before)

    def test_reconcile_on_start_self_heals(self):
        source = self._add_source()
        jm = self._manager(FakeRunner(self.workspace, entities=("Pydantic",)))
        self._run_one(jm, source["id"], source["title"])
        orphan = self.workspace / "wiki" / "sources" / "leftover.md"
        orphan.write_text("# leftover\n\nSource: gone\n", encoding="utf-8")

        # A fresh manager with reconcile_on_start should clean the orphan on boot.
        JobManager(self.workspace, FakeRunner(self.workspace), self.pipeline,
                   enrich_on_index=False, reconcile_on_start=True, autostart=False)
        self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
