from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT
APP = ROOT / "apps" / "learning-os"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(CORE))

import source_pipeline as source_pipeline_mod  # noqa: E402
from app_bridge import LexiconBridge  # noqa: E402

from spiritus.config import AppConfig, WorkspaceFolder  # noqa: E402


class FakeServer:
    def __init__(self, home_dir: Path):
        self.port = 4321
        self.home_dir = home_dir

    def stop(self):
        pass

    def start(self):
        return self.port


def make_bridge(tmp: Path) -> LexiconBridge:
    import os

    os.environ["WORKSPACE_PATH"] = str(tmp / "workspace")
    cfg = AppConfig(
        app_id="lexicon-test",
        app_title="Lexicon.ai",
        app_root=tmp,
        workspace_dirname="workspace",
        workspace_folders=(
            WorkspaceFolder("raw", "inbox", "raw"),
            WorkspaceFolder("processed", "file-check-2", "processed"),
            WorkspaceFolder("wiki", "brain", "wiki"),
        ),
    )
    server = FakeServer(tmp / ".opencode-home")
    return LexiconBridge(cfg, server)


class LexiconBridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # lexicon-indexer is a daemon thread and may still be writing
        self.tmp = Path(self._tmpdir.name)
        self.bridge = make_bridge(self.tmp)

    def tearDown(self):
        import os

        os.environ.pop("WORKSPACE_PATH", None)
        self._tmpdir.cleanup()

    def test_import_files_through_bridge(self):
        source = self.tmp / "paper.md"
        source.write_text("# Paper\n\nTransformers everywhere.\n", encoding="utf-8")

        result = self.bridge.lexicon_import_files([str(source)])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sources"]), 1)
        created = result["sources"][0]["source"]
        self.assertEqual(created["status"], "processed")
        self.assertIn("Transformers everywhere.", created["processed_markdown"])

    def test_import_url_through_bridge(self):
        original_get = source_pipeline_mod.requests.get

        def fake_get(url: str, timeout: int, headers: dict):
            return SimpleNamespace(
                status_code=200,
                url="https://example.com/post",
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html><head><title>Example Post</title></head><body><h1>Post</h1><p>Useful note.</p></body></html>",
                text="<html><head><title>Example Post</title></head><body><h1>Post</h1><p>Useful note.</p></body></html>",
                raise_for_status=lambda: None,
            )

        source_pipeline_mod.requests.get = fake_get
        try:
            result = self.bridge.lexicon_import_url("example.com/post")
        finally:
            source_pipeline_mod.requests.get = original_get

        self.assertTrue(result["ok"])
        source = result["source"]
        self.assertEqual(source["title"], "Example Post")
        self.assertEqual(source["format"], "html")
        self.assertIn("Useful note.", source["processed_markdown"])

    def test_overview_counts_imported_sources(self):
        note = self.tmp / "note.txt"
        note.write_text("hello world", encoding="utf-8")
        self.bridge.lexicon_import_files([str(note)])

        overview = self.bridge.lexicon_overview()
        self.assertEqual(overview["raw_count"], 1)
        self.assertEqual(overview["processed_count"], 1)

    def test_wiki_index_and_page_through_bridge(self):
        wiki = self.tmp / "workspace" / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "alpha.md").write_text("# Alpha\n\nSee [[Beta]].\n", encoding="utf-8")
        (wiki / "beta.md").write_text("# Beta\n\nStands alone.\n", encoding="utf-8")

        index = self.bridge.lexicon_wiki_index()
        self.assertTrue(index["ok"])
        self.assertEqual(len(index["pages"]), 2)
        self.assertEqual(index["edges"], [{"source": "alpha.md", "target": "beta.md"}])

        page = self.bridge.lexicon_wiki_page("beta.md")
        self.assertTrue(page["ok"])
        self.assertEqual(page["page"]["backlinks"], [{"path": "alpha.md", "title": "Alpha"}])

    def test_wiki_page_rejects_traversal(self):
        result = self.bridge.lexicon_wiki_page("../raw/secrets.md")
        self.assertFalse(result["ok"])

    def test_delete_source_through_bridge_removes_files(self):
        source = self.tmp / "delete-me.md"
        source.write_text("# Delete me\n\nContent.\n", encoding="utf-8")
        imported = self.bridge.lexicon_import_files([str(source)])
        source_id = imported["sources"][0]["source"]["id"]

        preview = self.bridge.lexicon_preview_delete(source_id)
        self.assertTrue(preview["ok"])

        result = self.bridge.lexicon_delete_source(source_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["files_deleted"])

        remaining = self.bridge.lexicon_list_sources()
        self.assertFalse(any(s["id"] == source_id for s in remaining))

    def test_preview_delete_through_bridge_unknown_source_errors(self):
        result = self.bridge.lexicon_preview_delete("src-does-not-exist")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
