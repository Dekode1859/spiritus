from __future__ import annotations

from knowledge import AgentRunner, JobManager
from source_pipeline import SourcePipeline
from wiki_library import WikiLibrary

from spiritus.bridge import Bridge


class LexiconBridge(Bridge):
    def __init__(self, config, server):
        super().__init__(config, server)
        self._sources = SourcePipeline(self._workspace)
        self._wiki = WikiLibrary(self._workspace)
        self._runner = AgentRunner(server, self._workspace)
        self._jobs = JobManager(self._workspace, self._runner, self._sources)

    # ── Sources ──────────────────────────────────────────────────────────────
    def lexicon_overview(self) -> dict:
        return self._sources.overview()

    def lexicon_list_sources(self) -> list[dict]:
        return self._sources.list_sources()

    def lexicon_get_source(self, source_id: str) -> dict:
        return self._sources.get_source(source_id)

    def lexicon_import_files(self, paths: list[str]) -> dict:
        result = self._sources.import_files(paths or [])
        for entry in result.get("sources", []):
            self._enqueue_from(entry.get("source"))
        return result

    def lexicon_import_url(self, url: str) -> dict:
        try:
            result = self._sources.import_url(url or "")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._enqueue_from(result.get("source"))
        return result

    def lexicon_reprocess_source(self, source_id: str) -> dict:
        try:
            result = self._sources.process_source(source_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._enqueue_from(result.get("source"))
        return result

    # ── Wiki ─────────────────────────────────────────────────────────────────
    def lexicon_wiki_index(self) -> dict:
        try:
            return self._wiki.index()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lexicon_wiki_page(self, path: str) -> dict:
        try:
            return self._wiki.page(path or "")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Knowledge build ──────────────────────────────────────────────────────
    def lexicon_knowledge_status(self) -> dict:
        try:
            return self._jobs.status()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lexicon_rebuild_all(self) -> dict:
        return self._jobs.rebuild_all()

    def lexicon_rebuild_source(self, source_id: str) -> dict:
        return self._jobs.rebuild_source(source_id or "")

    def lexicon_retry_failed(self) -> dict:
        return self._jobs.retry_failed()

    def lexicon_curate(self) -> dict:
        try:
            return self._jobs.curate()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lexicon_enrich(self) -> dict:
        try:
            return self._jobs.enrich(only_missing=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lexicon_reconcile(self) -> dict:
        try:
            return self._jobs.reconcile()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lexicon_entities(self) -> dict:
        try:
            return self._jobs.entities()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Delete ───────────────────────────────────────────────────────────────
    def lexicon_preview_delete(self, source_id: str) -> dict:
        try:
            return self._jobs.preview_delete(source_id or "")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lexicon_delete_source(self, source_id: str) -> dict:
        try:
            return self._jobs.forget_source(source_id or "")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Internals ────────────────────────────────────────────────────────────
    def _enqueue_from(self, source: dict | None) -> None:
        """Queue a background knowledge-build for a freshly processed source."""
        if source and source.get("status") == "processed":
            self._jobs.enqueue(source["id"], source.get("title", ""))
