"""
Job ingestion v2 (docs/scraper-v2/PLAN.md): one pipeline, split by source,
never by role. Career family, experience level and location are tags computed
after a posting is fetched, not filters on what gets fetched.

  taxonomy.py   experience levels and career families, and the legacy maps
  core/         what every source shares: HTTP conduct (core/http.py)
  adapters/     one module per source platform, found by adapters/__init__.py

The package imports nothing at load, so importing one module of it (the
taxonomy, read on every ingest) never pulls in the rest.
"""
