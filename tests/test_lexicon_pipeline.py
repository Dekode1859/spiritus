from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "apps" / "learning-os"
sys.path.insert(0, str(APP_ROOT))

from source_pipeline import SourcePipeline  # noqa: E402


class LexiconPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # lexicon-indexer is a daemon thread and may still be writing
        self.workspace = Path(self._tmpdir.name) / "workspace"
        self.pipeline = SourcePipeline(self.workspace)
        self.fixture_dir = Path(self._tmpdir.name) / "fixtures"
        self.fixture_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_text_file_import_creates_raw_and_processed_layers(self):
        path = self.fixture_dir / "notes.txt"
        path.write_text("Gradient descent updates parameters.\n", encoding="utf-8")

        result = self.pipeline.import_file(path)
        self.assertTrue(result["ok"])
        source = result["source"]
        self.assertEqual(source["format"], "text")
        self.assertEqual(source["status"], "processed")
        self.assertIn("Gradient descent updates parameters.", source["processed_markdown"])
        self.assertTrue((self.workspace / source["processed_path"]).exists())

    def test_html_import_extracts_title_and_body(self):
        path = self.fixture_dir / "article.html"
        path.write_text(
            "<html><head><title>Transformer Notes</title></head>"
            "<body><h1>Attention</h1><p>Sequence modeling without recurrence.</p></body></html>",
            encoding="utf-8",
        )

        result = self.pipeline.import_file(path)
        source = result["source"]
        self.assertEqual(source["title"], "Transformer Notes")
        self.assertIn("Sequence modeling without recurrence.", source["processed_markdown"])
        self.assertIn("Attention", source["processed_markdown"])

    def test_csv_import_preserves_full_content_in_markdown(self):
        path = self.fixture_dir / "papers.csv"
        path.write_text("title,year\nAttention,2017\nGPT,2018\n", encoding="utf-8")

        result = self.pipeline.import_file(path)
        markdown = result["source"]["processed_markdown"]
        self.assertIn("## Sample", markdown)
        self.assertIn("```csv", markdown)
        self.assertIn("Attention,2017", markdown)

    def test_windows_url_shortcut_routes_to_url_import(self):
        path = self.fixture_dir / "karpathy.url"
        path.write_text("[InternetShortcut]\nURL=https://example.com/wiki\n", encoding="utf-8")

        captured = {}

        def fake_import(url: str):
            captured["url"] = url
            return {"ok": True, "source": {"id": "src-demo"}}

        self.pipeline.import_url = fake_import  # type: ignore[method-assign]
        result = self.pipeline.import_file(path)
        self.assertTrue(result["ok"])
        self.assertEqual(captured["url"], "https://example.com/wiki")

    def test_unsupported_extension_raises(self):
        path = self.fixture_dir / "archive.bin"
        path.write_bytes(b"abc")

        with self.assertRaises(ValueError):
            self.pipeline.import_file(path)

    def test_duplicate_file_import_is_idempotent(self):
        path = self.fixture_dir / "note.txt"
        path.write_text("Same source bytes.\n", encoding="utf-8")

        first = self.pipeline.import_file(path)
        second = self.pipeline.import_file(path)

        self.assertEqual(first["source"]["id"], second["source"]["id"])
        self.assertEqual(first["source"]["imported_at"], second["source"]["imported_at"])
        self.assertEqual(len(self.pipeline.list_sources()), 1)

    def test_delete_source_removes_raw_and_processed(self):
        path = self.fixture_dir / "notes.txt"
        path.write_text("Delete me.\n", encoding="utf-8")
        source = self.pipeline.import_file(path)["source"]
        source_id = source["id"]

        self.assertTrue((self.workspace / "raw" / source_id).exists())
        self.assertTrue((self.workspace / "processed" / source_id).exists())

        result = self.pipeline.delete_source(source_id)
        self.assertTrue(result["ok"])
        self.assertFalse((self.workspace / "raw" / source_id).exists())
        self.assertFalse((self.workspace / "processed" / source_id).exists())
        self.assertEqual(self.pipeline.list_sources(), [])

    def test_delete_unknown_source_returns_error(self):
        result = self.pipeline.delete_source("src-does-not-exist")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_same_url_with_different_content_creates_distinct_sources(self):
        original_get = SourcePipeline.import_url.__globals__["requests"].get
        payloads = [
            b"<html><head><title>Post</title></head><body><p>Version one.</p></body></html>",
            b"<html><head><title>Post</title></head><body><p>Version two.</p></body></html>",
        ]

        class FakeResponse:
            def __init__(self, payload: bytes):
                self.url = "https://example.com/post"
                self.headers = {"content-type": "text/html; charset=utf-8"}
                self.content = payload
                self.text = payload.decode("utf-8")

            def raise_for_status(self):
                return None

        def fake_get(url: str, timeout: int, headers: dict):
            return FakeResponse(payloads.pop(0))

        SourcePipeline.import_url.__globals__["requests"].get = fake_get
        try:
            first = self.pipeline.import_url("https://example.com/post")
            second = self.pipeline.import_url("https://example.com/post")
        finally:
            SourcePipeline.import_url.__globals__["requests"].get = original_get

        self.assertNotEqual(first["source"]["id"], second["source"]["id"])
        self.assertEqual(len(self.pipeline.list_sources()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
