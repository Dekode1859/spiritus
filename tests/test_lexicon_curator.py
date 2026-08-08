from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "apps" / "learning-os"
sys.path.insert(0, str(APP_ROOT))

from curator import Curator  # noqa: E402
from wiki_library import WikiLibrary  # noqa: E402


class CuratorTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # lexicon-indexer is a daemon thread and may still be writing
        self.workspace = Path(self._tmpdir.name) / "workspace"
        self.lexicon_sources = self.workspace / "wiki" / ".lexicon" / "sources"
        self.lexicon_sources.mkdir(parents=True, exist_ok=True)
        (self.workspace / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
        self.curator = Curator(self.workspace)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _manifest(self, source_id, title, entities, relations=(), note_body="body"):
        # Entity dicts default to scope="general" unless a test sets it, matching
        # the Curator's _norm_scope default (so most tests stay concise).
        note_page = f"sources/{source_id}.md"
        note = self.workspace / "wiki" / note_page
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# {title}\n\n{note_body}\n\nSource: {source_id}\n", encoding="utf-8")
        manifest = {
            "source_id": source_id, "title": title, "note_page": note_page,
            "entities": entities,
            "relations": [{"from": f, "to": t, "explanation": ex} for (f, t, ex) in relations],
        }
        (self.lexicon_sources / f"{source_id}.json").write_text(
            json.dumps(manifest), encoding="utf-8")

    def _registry(self):
        path = self.workspace / "wiki" / ".lexicon" / "entities.json"
        return {e["id"]: e for e in json.loads(path.read_text(encoding="utf-8"))["entities"]}

    # ── Identity: name-only, type is a free label chosen by majority vote ────

    def test_same_name_merges_across_sources_regardless_of_type_wording(self):
        self._manifest("src-a", "Paper A", [
            {"name": "Transformer", "type": "framework", "aliases": ["transformers"], "evidence": "attention model"}])
        self._manifest("src-b", "Paper B", [
            {"name": "transformer", "type": "neural network architecture", "aliases": ["xformer"], "evidence": "seq model"}])

        result = self.curator.curate()
        self.assertTrue(result["ok"])
        registry = self._registry()
        self.assertEqual(len(registry), 1)          # one entity, not two — no type partitioning
        entity = next(iter(registry.values()))
        self.assertEqual(entity["id"], "transformer")
        self.assertEqual(len(entity["sources"]), 2)
        self.assertIn("transformers", entity["aliases"])
        self.assertIn("xformer", entity["aliases"])

    def test_canonical_type_is_majority_vote_across_mentions(self):
        self._manifest("src-a", "A", [{"name": "React", "type": "library", "evidence": "x"}])
        self._manifest("src-b", "B", [{"name": "React", "type": "library", "evidence": "y"}])
        self._manifest("src-c", "C", [{"name": "React", "type": "framework", "evidence": "z"}])
        self.curator.curate()
        registry = self._registry()
        self.assertEqual(next(iter(registry.values()))["type"], "library")   # 2 votes beats 1

    def test_type_is_a_free_label_not_a_fixed_enum(self):
        self._manifest("src-a", "A", [{"name": "Something New", "type": "protocol", "evidence": "x"}])
        self.curator.curate()
        registry = self._registry()
        self.assertEqual(next(iter(registry.values()))["type"], "protocol")   # not clamped to a known set

    def test_missing_type_falls_back_to_topic(self):
        self._manifest("src-a", "A", [{"name": "Mystery", "type": "", "evidence": "x"}])
        self.curator.curate()
        registry = self._registry()
        self.assertEqual(next(iter(registry.values()))["type"], "topic")

    # ── Scope ────────────────────────────────────────────────────────────────

    def test_local_scope_entities_are_not_promoted(self):
        self._manifest("src-a", "A", [
            {"name": "Pydantic", "type": "library", "scope": "general", "evidence": "x"},
            {"name": "validator node", "type": "concept", "scope": "local", "evidence": "y"},
        ])
        self.curator.curate()
        registry = self._registry()
        names = {e["name"] for e in registry.values()}
        self.assertEqual(names, {"Pydantic"})           # local jargon stays out

    def test_relations_touching_local_entities_are_dropped(self):
        self._manifest("src-a", "A", [
            {"name": "Pydantic", "type": "library", "scope": "general", "evidence": "x"},
            {"name": "validator node", "type": "concept", "scope": "local", "evidence": "y"},
        ], relations=[("Pydantic", "validator node", "validates its input")])
        self.curator.curate()
        relations = json.loads(
            (self.workspace / "wiki" / ".lexicon" / "relations.json").read_text(encoding="utf-8"))
        self.assertEqual(relations["relations"], [])    # can't relate to un-promoted local term

    # ── Relations: an explanation is required, not a coded type ──────────────

    def test_relations_carry_a_grounded_explanation_not_a_type_code(self):
        self._manifest("src-a", "A", [
            {"name": "Pydantic", "type": "library", "evidence": "x"},
            {"name": "data validation", "type": "concept", "evidence": "y"},
        ], relations=[("Pydantic", "data validation", "Pydantic enforces the shape of incoming JSON before it reaches the handler")])
        self.curator.curate()
        relations = json.loads(
            (self.workspace / "wiki" / ".lexicon" / "relations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(relations["relations"]), 1)
        rel = relations["relations"][0]
        self.assertNotIn("type", rel)
        self.assertEqual(rel["explanation"], "Pydantic enforces the shape of incoming JSON before it reaches the handler")
        self.assertEqual(rel["from"], "pydantic")
        self.assertEqual(rel["to"], "data-validation")
        self.assertIn("src-a", rel["sources"])

    def test_relation_with_no_explanation_is_dropped(self):
        self._manifest("src-a", "A", [
            {"name": "Alpha", "type": "concept", "evidence": "x"},
            {"name": "Beta", "type": "concept", "evidence": "y"},
        ], relations=[("Alpha", "Beta", "")])   # empty explanation — not a real relation
        self.curator.curate()
        relations = json.loads(
            (self.workspace / "wiki" / ".lexicon" / "relations.json").read_text(encoding="utf-8"))
        self.assertEqual(relations["relations"], [])

    # ── Entity page structure ────────────────────────────────────────────────

    def test_entity_page_leads_with_description_then_sources_then_relationships(self):
        self._manifest("src-a", "Attention Paper", [
            {"name": "PyTorch", "type": "library", "evidence": "used to train the model"},
            {"name": "Gradient Descent", "type": "concept", "evidence": "the optimizer used"},
        ], relations=[("PyTorch", "Gradient Descent",
                       "PyTorch's autograd computes the gradients that gradient descent applies")])
        self.curator.curate()
        body = (self.workspace / "wiki" / "entities" / "library" / "pytorch.md").read_text(encoding="utf-8")

        # Description-first: no separate "What it is" heading, straight under H1.
        self.assertTrue(body.startswith("# PyTorch\n\n_Not enriched yet"))
        self.assertIn("## Sources", body)
        self.assertIn("[Attention Paper](../../sources/src-a.md) — used to train the model", body)
        self.assertIn("## Relationships", body)
        self.assertIn("[[Gradient Descent]] — PyTorch's autograd computes the gradients "
                      "that gradient descent applies", body)
        # Order matters: description/sources before relationships.
        self.assertLess(body.index("## Sources"), body.index("## Relationships"))

    def test_enriched_entity_shows_description_and_external_sources(self):
        self._manifest("src-a", "A", [{"name": "Transformer", "type": "framework", "evidence": "x"}])
        self.curator.curate()
        path = self.workspace / "wiki" / ".lexicon" / "entities.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entities"][0]["enrichment"] = {
            "description": "A landmark 2017 neural network architecture based on self-attention, "
                          "underpinning modern large language models like GPT and BERT.",
            "external_sources": [{"label": "Attention Is All You Need (paper)",
                                  "url": "https://arxiv.org/abs/1706.03762"}],
            "confidence": "high",
        }
        path.write_text(json.dumps(data), encoding="utf-8")

        self.curator.curate()
        body = (self.workspace / "wiki" / "entities" / "framework" / "transformer.md").read_text(encoding="utf-8")
        self.assertTrue(body.startswith(
            "# Transformer\n\nA landmark 2017 neural network architecture based on self-attention,"))
        self.assertIn("## External Sources", body)
        self.assertIn("Recalled from the model's own knowledge, not fetched live", body)
        self.assertIn("[Attention Is All You Need (paper)](https://arxiv.org/abs/1706.03762)", body)

    def test_unenriched_entity_has_no_external_sources_section(self):
        self._manifest("src-a", "A", [{"name": "Alpha", "type": "concept", "evidence": "x"}])
        self.curator.curate()
        body = (self.workspace / "wiki" / "entities" / "concept" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("## External Sources", body)

    def test_multiple_source_mentions_each_get_their_own_bullet(self):
        self._manifest("src-a", "Paper A", [{"name": "Alpha", "type": "concept", "evidence": "seen in A"}])
        self._manifest("src-b", "Paper B", [{"name": "Alpha", "type": "concept", "evidence": "seen in B"}])
        self.curator.curate()
        body = (self.workspace / "wiki" / "entities" / "concept" / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("[Paper A](../../sources/src-a.md) — seen in A", body)
        self.assertIn("[Paper B](../../sources/src-b.md) — seen in B", body)

    def test_entity_pages_link_to_source_notes_as_graph_edges(self):
        self._manifest("src-a", "Attention Paper", [
            {"name": "Transformer", "type": "framework", "evidence": "arch"},
            {"name": "Self Attention", "type": "concept", "evidence": "mechanism"},
        ], relations=[("Transformer", "Self Attention", "Transformer layers are built from self-attention blocks")])
        self.curator.curate()
        index = WikiLibrary(self.workspace).index()
        edges = {(e["source"], e["target"]) for e in index["edges"]}
        self.assertIn(("entities/framework/transformer.md", "sources/src-a.md"), edges)
        self.assertIn(("entities/framework/transformer.md", "entities/concept/self-attention.md"), edges)
        self.assertFalse(any(".lexicon" in p["path"] for p in index["pages"]))

    # ── Entity index: a self-sufficient metadata layer ────────────────────────

    def test_entity_index_includes_description_and_per_source_lines(self):
        self._manifest("src-a", "Paper A", [{"name": "Pydantic", "type": "library", "evidence": "validates input"}])
        path = self.workspace / "wiki" / ".lexicon" / "entities.json"
        self.curator.curate()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entities"][0]["enrichment"] = {
            "description": "Pydantic is a Python data validation library.",
            "external_sources": [], "confidence": "high",
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        self.curator.curate()

        index_body = (self.workspace / "wiki" / "indexes" / "entities.md").read_text(encoding="utf-8")
        self.assertIn("[[Pydantic]]", index_body)
        self.assertIn("Pydantic is a Python data validation library.", index_body)
        self.assertIn("[Paper A](sources/src-a.md) — validates input", index_body)

    def test_entity_index_shows_relationship_count(self):
        self._manifest("src-a", "A", [
            {"name": "Pydantic", "type": "library", "evidence": "x"},
            {"name": "FastAPI", "type": "framework", "evidence": "y"},
        ], relations=[("FastAPI", "Pydantic", "FastAPI uses Pydantic models for request validation")])
        self.curator.curate()
        index_body = (self.workspace / "wiki" / "indexes" / "entities.md").read_text(encoding="utf-8")
        self.assertIn("1 relationship", index_body)

    def test_registry_reports_counts_by_type(self):
        self._manifest("src-a", "A", [
            {"name": "PyTorch", "type": "library", "evidence": "x"},
            {"name": "Adam", "type": "tool", "evidence": "y"},
            {"name": "Backprop", "type": "concept", "evidence": "z"},
        ])
        self.curator.curate()
        registry = self.curator.registry()
        self.assertTrue(registry["ok"])
        self.assertEqual(registry["total"], 3)
        self.assertEqual(registry["by_type"], {"library": 1, "tool": 1, "concept": 1})

    def test_empty_workspace_curates_cleanly(self):
        result = self.curator.curate()
        self.assertTrue(result["ok"])
        self.assertEqual(result["entities"], 0)
        index_page = self.workspace / "wiki" / "indexes" / "entities.md"
        self.assertFalse(index_page.exists())

    # ── impact_of_source (delete preview) ────────────────────────────────────

    def test_impact_of_unindexed_source_is_empty(self):
        impact = self.curator.impact_of_source("src-never-seen")
        self.assertTrue(impact["ok"])
        self.assertEqual(impact["entities_removed"], [])
        self.assertEqual(impact["entities_affected"], [])
        self.assertEqual(impact["relations_removed"], 0)

    def test_impact_identifies_entity_removed_when_source_is_sole_owner(self):
        self._manifest("src-a", "A", [{"name": "Unique Thing", "type": "concept", "evidence": "x"}])
        impact = self.curator.impact_of_source("src-a")
        self.assertEqual([e["name"] for e in impact["entities_removed"]], ["Unique Thing"])
        self.assertEqual(impact["entities_affected"], [])

    def test_impact_identifies_entity_affected_when_shared_across_sources(self):
        self._manifest("src-a", "A", [{"name": "Shared Thing", "type": "concept", "evidence": "x"}])
        self._manifest("src-b", "B", [{"name": "Shared Thing", "type": "concept", "evidence": "y"}])
        impact = self.curator.impact_of_source("src-a")
        self.assertEqual(impact["entities_removed"], [])
        self.assertEqual([e["name"] for e in impact["entities_affected"]], ["Shared Thing"])

    def test_impact_counts_relations_removed(self):
        self._manifest("src-a", "A", [
            {"name": "Alpha", "type": "concept", "evidence": "x"},
            {"name": "Beta", "type": "concept", "evidence": "y"},
        ], relations=[("Alpha", "Beta", "Alpha feeds its output into Beta")])
        impact = self.curator.impact_of_source("src-a")
        self.assertEqual(impact["relations_removed"], 1)

    def test_impact_is_pure_and_does_not_write_anything(self):
        self._manifest("src-a", "A", [{"name": "Alpha", "type": "concept", "evidence": "x"}])
        registry_path = self.workspace / "wiki" / ".lexicon" / "entities.json"
        self.assertFalse(registry_path.exists())
        self.curator.impact_of_source("src-a")
        self.assertFalse(registry_path.exists())   # curate() was never called

    def test_impact_predicts_forget_source_effect_exactly(self):
        self._manifest("src-a", "A", [
            {"name": "OnlyInA", "type": "concept", "evidence": "x"},
            {"name": "Shared", "type": "concept", "evidence": "x"},
        ])
        self._manifest("src-b", "B", [{"name": "Shared", "type": "concept", "evidence": "y"}])
        self.curator.curate()   # establish the "before" canonical state

        impact = self.curator.impact_of_source("src-a")
        removed_names = {e["name"] for e in impact["entities_removed"]}
        self.assertEqual(removed_names, {"OnlyInA"})

        # Simulate what forget_source does: drop the manifest, re-curate.
        (self.workspace / "wiki" / ".lexicon" / "sources" / "src-a.json").unlink()
        self.curator.curate()
        registry = self._registry()
        self.assertNotIn("onlyina", registry)
        self.assertIn("shared", registry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
