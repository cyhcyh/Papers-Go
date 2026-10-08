# Runtime classification resources

`research_areas.json` is the independent, medium-grained research-area seed (85 Mathematics and 77 Computer Science entries). It was supplied from the taxonomy project on 2026-10-04. Every entry has exactly `id`, `discipline`, `name`, `description`.

Startup imports the seed once into the shared database. Administrator edits and approvals update the catalog in the same transaction as website topics. The current four-field table is exported atomically to `DATA_DIR/research_areas.json` after commit and on startup; this persistent copy survives Docker rebuilds. New IDs are allocated by the application after approval; editing keeps IDs stable. Chinese display names, source memberships, proposal reasons, and status are stored only in the website database.

arXiv source categories are separately described by `arxiv_categories.json`. They control ingestion and browsing and do not restrict the scientific discipline of research-area candidates.
