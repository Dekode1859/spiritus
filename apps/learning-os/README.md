# Lexicon.ai

Lexicon.ai is a Spiritus application for building a personal knowledge base in
three layers:

- `raw/` stores immutable source artifacts
- `processed/` stores deterministic markdown conversions of those sources
- `wiki/` is the LLM-maintained knowledge graph (pages, weekly folders, backlinks)

The app ships its own UI (Dashboard, Library, Wiki, Graph) and its own bridge
extension, but it runs on the same Spiritus Core as the other apps in this
repository.

## Supported source formats

- PDF
- EPUB
- DOCX
- TXT
- Markdown
- HTML
- JSON
- CSV / TSV
- direct web URLs

## Run

```bash
make install
make run
```

The ingest pipeline is deterministic and app-local. Raw files are never
rewritten. Processed markdown is regenerated from the raw layer whenever you
reprocess a source. The wiki index (links, backlinks, and the graph edge list)
is computed on demand from the markdown in `wiki/`.
