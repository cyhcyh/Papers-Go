"""Indexed display dates; a conference month is never a publication day."""
from .config import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS conference_editions (
 source_key TEXT NOT NULL REFERENCES source_categories(key) ON DELETE CASCADE,
 venue TEXT NOT NULL COLLATE NOCASE, year INTEGER NOT NULL,
 date_month TEXT, source_url TEXT, checked_at TEXT NOT NULL, dates_ready INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(venue,year));
CREATE TABLE IF NOT EXISTS conference_entry_versions (
 source_key TEXT NOT NULL REFERENCES source_categories(key) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES paper_sources(source_id) ON DELETE CASCADE,
 digest TEXT NOT NULL, PRIMARY KEY(source_key,source_id));
"""


def expressions(prefix=''):
    p = prefix + '.' if prefix else ''
    venue = f"CASE WHEN INSTR({p}venue,'.')>0 THEN SUBSTR({p}venue,1,INSTR({p}venue,'.')-1) ELSE {p}venue END"
    month = f'(SELECT date_month FROM conference_editions WHERE venue=({venue}) COLLATE NOCASE AND year={p}venue_year)'
    published = f"NULLIF({p}published,'')"
    year = f"CASE WHEN {p}venue IS NOT NULL AND {p}venue_year IS NOT NULL THEN CAST({p}venue_year AS TEXT) END"
    value = f'COALESCE({published},{month},{year},{p}ingested_date)'
    basis = f"CASE WHEN {published} IS NOT NULL THEN 'publication' WHEN {month} IS NOT NULL THEN 'conference' WHEN {year} IS NOT NULL THEN 'year' ELSE 'ingested' END"
    # 00 represents unknown precision for ordering only; it is not a stored day.
    sort = f"CASE LENGTH({value}) WHEN 4 THEN ({value})||'-00-00' WHEN 7 THEN ({value})||'-00' ELSE ({value}) END"
    return value, basis, sort


def initialize(db):
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO app_settings(name,value,updated_at) VALUES('paper_date_revision','0',?)",(now(),))
    columns = {r['name'] for r in db.execute('PRAGMA table_info(papers)')}
    for name in ('paper_date','paper_date_basis','paper_date_sort'):
        if name not in columns:
            db.execute('ALTER TABLE papers ADD COLUMN '+name+' TEXT')
    value,basis,sort = expressions('NEW')
    update = f'UPDATE papers SET paper_date={value},paper_date_basis={basis},paper_date_sort={sort} WHERE id=NEW.id;'
    db.execute('CREATE TRIGGER IF NOT EXISTS paper_date_insert AFTER INSERT ON papers BEGIN '+update+' END')
    db.execute('CREATE TRIGGER IF NOT EXISTS paper_date_update AFTER UPDATE OF published,venue,venue_year,ingested_date ON papers BEGIN '+update+' END')
    if not db.execute("SELECT 1 FROM app_migrations WHERE name='paper_dates_v1'").fetchone():
        value,basis,sort = expressions('papers')
        db.execute(f'UPDATE papers SET paper_date={value},paper_date_basis={basis},paper_date_sort={sort}')
        db.execute("INSERT INTO app_migrations VALUES('paper_dates_v1',?)",(now(),))
    db.execute('CREATE INDEX IF NOT EXISTS idx_papers_paper_date ON papers(paper_date_sort DESC,id DESC)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_papers_ingested_sort ON papers(ingested_date DESC,id DESC)')


def refresh_batch(db, ids):
    """Refresh one bounded batch without touching ingestion, expiry or model outputs."""
    if not ids:return 0
    value,basis,sort = expressions('papers')
    changed = db.execute(f'UPDATE papers SET paper_date={value},paper_date_basis={basis},paper_date_sort={sort} '
                      'WHERE id IN ('+','.join('?' for _ in ids)+') '
                      f'AND (paper_date IS NOT ({value}) OR paper_date_basis IS NOT ({basis}))',ids).rowcount
    if changed:
        # Only cached date-filtered browsing depends on this metadata. Do not
        # repeatedly invalidate every user's recommendation cache during backfill.
        db.execute("UPDATE app_settings SET value=CAST(value AS INTEGER)+1 WHERE name='paper_date_revision'")
    return changed
