import json
import sqlite3
import struct
import math
from contextlib import contextmanager
from .config import settings, now
from .models import SCHEMA


@contextmanager
def connect(*, background=False):
    cfg = settings()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.data_dir / 'papers.sqlite3', timeout=30, uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute(f'PRAGMA mmap_size={cfg.sqlite_mmap_mb * 1024 * 1024}')
    if background:
        db.execute('PRAGMA cache_size=-131072')
    import sqlite_vec
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(recover=True):
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        from .llm.catalog import SCHEMA as CATALOG_SCHEMA
        from .llm.codex import SCHEMA as CODEX_SCHEMA
        from .llm.embedding_queue import SCHEMA as EMBEDDING_QUEUE_SCHEMA
        from .llm.vector_rebuild import SCHEMA as REBUILD_SCHEMA
        for schema in (CATALOG_SCHEMA,CODEX_SCHEMA,EMBEDDING_QUEUE_SCHEMA,REBUILD_SCHEMA): db.executescript(schema)
        from .vector_store import SCHEMA as VECTOR_STORE_SCHEMA
        db.executescript(VECTOR_STORE_SCHEMA)
        from .interest.profile_updates import SCHEMA as INTEREST_UPDATE_SCHEMA
        db.executescript(INTEREST_UPDATE_SCHEMA)
        from .pipeline.redo import SCHEMA as REDO_SCHEMA
        db.executescript(REDO_SCHEMA)
        if 'redo_id' not in {r['name'] for r in db.execute('PRAGMA table_info(pipeline_commands)')}:
            db.execute('ALTER TABLE pipeline_commands ADD COLUMN redo_id INTEGER')
        if recover:
            db.execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE status IN ('running','queued')")
        usage_columns = {r['name'] for r in db.execute('PRAGMA table_info(llm_usage)')}
        if 'billing_source' not in usage_columns:
            db.execute("ALTER TABLE llm_usage ADD COLUMN billing_source TEXT NOT NULL DEFAULT 'api'")
        for name, definition in [('feature','TEXT'),('connection_id','TEXT'),('usage_known','INTEGER NOT NULL DEFAULT 1')]:
            if name not in usage_columns:
                db.execute(f'ALTER TABLE llm_usage ADD COLUMN {name} {definition}')
        db.execute('CREATE INDEX IF NOT EXISTS idx_llm_usage_date ON llm_usage(created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_interactions_date ON interactions(created_at)')
        from .site_settings import initialize as initialize_site
        initialize_site(db)
        from .recommendation_settings import initialize as initialize_recommendation
        initialize_recommendation(db)
        if 'auth_epoch' not in {r['name'] for r in db.execute('PRAGMA table_info(users)')}:
            db.execute("ALTER TABLE users ADD COLUMN auth_epoch TEXT NOT NULL DEFAULT 'legacy'")
        db.execute("INSERT OR IGNORE INTO app_settings(name,value,updated_at) SELECT 'last_user_id',CAST(COALESCE(MAX(id),0) AS TEXT),? FROM users",(now(),))
        from .llm.secrets import migrate_credentials
        migrate_credentials(db)
        from .llm.vector_rebuild import migrate_legacy
        migrate_legacy(db)
        model_config = db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        if model_config:
            settings().embedding_dim = json.loads(model_config['value'])['embedding_dim']
        if 'progress_json' not in {r['name'] for r in db.execute('PRAGMA table_info(reading_cards)')}:
            db.execute('ALTER TABLE reading_cards ADD COLUMN progress_json TEXT')
        from .pipeline.reading_queue import initialize as initialize_reading_queue
        initialize_reading_queue(db)
        from .pipeline.fulltext_cache import initialize as initialize_fulltext_cache
        initialize_fulltext_cache(db)
        interaction_columns = {r['name'] for r in db.execute('PRAGMA table_info(interactions)')}
        if 'vector_epoch' not in interaction_columns:
            db.execute("ALTER TABLE interactions ADD COLUMN vector_epoch TEXT NOT NULL DEFAULT 'legacy'")
        if 'progress' not in {r['name'] for r in db.execute('PRAGMA table_info(source_status)')}:
            db.execute("ALTER TABLE source_status ADD COLUMN progress TEXT DEFAULT '{}'")
        if 'view_rule' not in interaction_columns:
            db.execute('ALTER TABLE interactions ADD COLUMN view_rule INTEGER NOT NULL DEFAULT 0')
        metric_columns = {r['name'] for r in db.execute('PRAGMA table_info(daily_metrics)')}
        if 'considered' not in metric_columns:
            db.execute('ALTER TABLE daily_metrics ADD COLUMN considered INTEGER NOT NULL DEFAULT 0')
            db.execute('UPDATE daily_metrics SET considered=shown')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(papers)')}
        if 'base_quality_score' not in columns:
            db.execute('ALTER TABLE papers ADD COLUMN base_quality_score REAL')
            db.execute('UPDATE papers SET base_quality_score=quality_score')
        if 'author_impact' not in columns:
            db.execute('ALTER TABLE papers ADD COLUMN author_impact REAL NOT NULL DEFAULT 0')
        db.executescript('''CREATE TRIGGER IF NOT EXISTS author_match_reset AFTER UPDATE OF title,authors ON papers
            WHEN NEW.title!=OLD.title OR NEW.authors!=OLD.authors BEGIN
            DELETE FROM author_work_matches WHERE paper_id=NEW.id;
            DELETE FROM paper_author_links WHERE paper_id=NEW.id;
            UPDATE papers SET author_impact=0,quality_score=COALESCE(base_quality_score,quality_score) WHERE id=NEW.id;
            END;''')
        for name, definition in [('categories', "TEXT DEFAULT '[]'"), ('brief_json', 'TEXT')]:
            if name not in columns:
                db.execute(f'ALTER TABLE papers ADD COLUMN {name} {definition}')
        if 'title_key' not in {r['name'] for r in db.execute('PRAGMA table_info(papers)')}:
            db.execute('ALTER TABLE papers ADD COLUMN title_key TEXT')
            import re
            db.executemany('UPDATE papers SET title_key=? WHERE id=?',
                           [(re.sub(r'\W+', '', p['title'].casefold()), p['id']) for p in db.execute('SELECT id,title FROM papers')])
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_title_key ON papers(title_key)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_venue ON papers(venue,id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_missing_vec ON papers(id) WHERE embedding IS NULL')
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published,id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_rank ON papers(quality_score DESC,published DESC,id DESC,ingested_date)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_recent ON papers(ingested_date DESC,id DESC,published,quality_score)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_papers_display_date ON papers(COALESCE(published,ingested_date) DESC,id DESC)')
        category_insert = """INSERT OR IGNORE INTO paper_categories(paper_id,category_key)
            SELECT NEW.id, 'venue:'||LOWER(CASE WHEN INSTR(NEW.venue,'.')>0 THEN SUBSTR(NEW.venue,1,INSTR(NEW.venue,'.')-1) ELSE NEW.venue END) WHERE NEW.venue IS NOT NULL;
            INSERT OR IGNORE INTO paper_categories(paper_id,category_key) SELECT NEW.id,'arxiv:'||LOWER(NEW.primary_category) WHERE NEW.venue IS NULL AND NEW.primary_category IS NOT NULL;
            INSERT OR IGNORE INTO paper_categories(paper_id,category_key) SELECT NEW.id,'arxiv:'||LOWER(value) FROM json_each(COALESCE(NEW.categories,'[]')) WHERE NEW.venue IS NULL;"""
        db.execute('CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        from .llm.runtime import initialize_explicit_selection
        initialize_explicit_selection(db)
        from .pipeline.quality import initialize as initialize_quality
        initialize_quality(db)
        from .pipeline.author_impact import initialize as initialize_authors
        initialize_authors(db)
        from .pipeline.redo import migrate_split_jobs
        migrate_split_jobs(db)
        if not db.execute("SELECT 1 FROM app_migrations WHERE name='paper_reading_v2'").fetchone():
            saved=db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
            if saved:
                config=json.loads(saved['value'])
                config['brief_cloud_concurrency']=4
                from .llm.controls import capabilities
                route=config.get('routes',{}).get('brief',{})
                for choice in (route.get('primary'),route.get('fallback')):
                    if not choice:continue
                    connection=next((c for c in config['connections'] if c['id']==choice['connection_id']),{})
                    if connection.get('kind')=='cloud' and 'off' in capabilities({**connection,**choice})['thinking_modes']:
                        choice['thinking']='off'
                        choice['reasoning_effort']='auto'
                db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='models'",(dumps(config),now()))
            db.execute("INSERT INTO app_migrations(name,applied_at) VALUES('paper_reading_v2',?)",(now(),))
        db.executescript('CREATE TRIGGER IF NOT EXISTS paper_category_insert AFTER INSERT ON papers BEGIN '+category_insert+' END;'
                         'CREATE TRIGGER IF NOT EXISTS paper_category_update AFTER UPDATE OF venue,primary_category,categories ON papers BEGIN DELETE FROM paper_categories WHERE paper_id=NEW.id; '+category_insert+' END;')
        if not db.execute("SELECT 1 FROM app_migrations WHERE name='paper_category_index_v1'").fetchone():
            db.execute("INSERT OR IGNORE INTO paper_categories SELECT id,'venue:'||LOWER(CASE WHEN INSTR(venue,'.')>0 THEN SUBSTR(venue,1,INSTR(venue,'.')-1) ELSE venue END) FROM papers WHERE venue IS NOT NULL")
            db.execute("INSERT OR IGNORE INTO paper_categories SELECT id,'arxiv:'||LOWER(primary_category) FROM papers WHERE venue IS NULL AND primary_category IS NOT NULL")
            db.execute("INSERT OR IGNORE INTO paper_categories SELECT p.id,'arxiv:'||LOWER(c.value) FROM papers p,json_each(COALESCE(p.categories,'[]')) c WHERE p.venue IS NULL")
            db.execute("INSERT INTO app_migrations(name,applied_at) VALUES('paper_category_index_v1',?)",(now(),))
        db.execute("INSERT OR IGNORE INTO app_settings(name,value,updated_at) VALUES('paper_revision','0',?)",(now(),))
        from .source_deletion_queue import initialize as initialize_deletions
        initialize_deletions(db)
        from .source_catalog import initialize as initialize_sources
        initialize_sources(db)
        for table in ('papers','topics','paper_topics','source_categories'):
            for operation in ('INSERT','UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS revision_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN UPDATE app_settings SET value=CAST(value AS INTEGER)+1 WHERE name='paper_revision'; END")
        if 'venue_year' not in {r['name'] for r in db.execute('PRAGMA table_info(papers)')}:
            db.execute('ALTER TABLE papers ADD COLUMN venue_year INTEGER')
        from .paper_dates import initialize as initialize_paper_dates
        initialize_paper_dates(db)
        if 'material_id' not in {r['name'] for r in db.execute('PRAGMA table_info(direction_trends)')}:
            db.execute('ALTER TABLE direction_trends ADD COLUMN material_id INTEGER')
        for name,definition in [('items_json',"TEXT NOT NULL DEFAULT '[]'"),('period_start','TEXT'),('period_end','TEXT'),('prompt_revision','TEXT'),('attempted_prompt_revision','TEXT'),('attempted_material_id','INTEGER'),('attempt_token','TEXT')]:
            if name not in {r['name'] for r in db.execute('PRAGMA table_info(direction_trends)')}:
                db.execute('ALTER TABLE direction_trends ADD COLUMN '+name+' '+definition)
        db.execute(f'CREATE VIRTUAL TABLE IF NOT EXISTS papers_vec USING vec0(paper_id INTEGER PRIMARY KEY, embedding float[{settings().embedding_dim}] distance_metric=cosine)')
        topic_columns = {r['name'] for r in db.execute('PRAGMA table_info(topics)')}
        if 'category_keys' not in topic_columns:
            db.execute('ALTER TABLE topics ADD COLUMN category_keys TEXT')
        for name in ('standard_key','standard_system','standard_code','standard_path'):
            if name not in {r['name'] for r in db.execute('PRAGMA table_info(topics)')}:
                db.execute(f'ALTER TABLE topics ADD COLUMN {name} TEXT')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_standard ON topics(standard_key) WHERE standard_key IS NOT NULL')
        if 'classification_state' not in {r['name'] for r in db.execute('PRAGMA table_info(papers)')}:
            db.execute("ALTER TABLE papers ADD COLUMN classification_state TEXT NOT NULL DEFAULT 'pending'")
        from .pipeline.arxiv_daily import initialize as initialize_daily
        initialize_daily(db)
        from .standard_topics import migrate
        migrate(db)
        from .research_catalog import synchronize
        synchronize(db)
        if 'embedding_parts' not in {r['name'] for r in db.execute('PRAGMA table_info(interest_profile)')}:
            db.execute('ALTER TABLE interest_profile ADD COLUMN embedding_parts TEXT')
        from .interest.profile import trim_history
        for user in db.execute('SELECT DISTINCT user_id FROM interest_profile').fetchall():
            trim_history(db, user['user_id'])
        from .interest.profile import migrate_background
        migrate_background(db)
        from .pipeline.alert_state import initialize as initialize_alerts
        initialize_alerts(db)
        from .paper_index import initialize as initialize_search
        initialize_search(db)
        from .pipeline.aggregate_state import initialize as initialize_aggregates
        initialize_aggregates(db)
        from .paper_lifecycle import initialize as initialize_lifetimes
        initialize_lifetimes(db)
        from .browsing import initialize as initialize_browsing
        initialize_browsing(db)
        # Social counts and lifetime changes do not change recommendation rankings.
        # Pages read these fields afresh when hydrating their cached paper IDs.
        db.execute('DROP TRIGGER IF EXISTS revision_papers_update')
        db.execute("""CREATE TRIGGER revision_papers_update AFTER UPDATE OF
            arxiv_id,arxiv_version,title,abstract,authors,categories,primary_category,venue,venue_rank,venue_year,
            published,ingested_date,quality_score,base_quality_score,author_impact,author_impact_known,hf_upvotes,github_stars,
            tldr,brief_json,classified,scored,skeleton ON papers BEGIN
            UPDATE app_settings SET value=CAST(value AS INTEGER)+1 WHERE name='paper_revision'; END""")
        db.execute('DROP TRIGGER IF EXISTS revision_papers_embedding_update')
        db.execute("""CREATE TRIGGER revision_papers_embedding_update AFTER UPDATE OF embedding ON papers
            WHEN NEW.embedding IS NOT X'01' OR OLD.embedding IS NULL BEGIN
            UPDATE app_settings SET value=CAST(value AS INTEGER)+1 WHERE name='paper_revision'; END""")
        from .agent_skills import initialize as initialize_skills
        initialize_skills(db)
        from .interest.profile_updates import initialize_upgrade
        initialize_upgrade(db)
        # A process interrupted during generation can retry safely.
        if recover:
            db.execute("UPDATE reading_cards SET status='failed', error='生成被中断，请重试' WHERE status='pending' AND paper_id NOT IN (SELECT paper_id FROM reading_jobs)")
            db.execute('UPDATE source_status SET running=0')


def rows(sql, args=()):
    with connect() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def one(sql, args=()):
    with connect() as db:
        row = db.execute(sql, args).fetchone()
        return dict(row) if row else None


def execute(sql, args=()):
    with connect() as db:
        return db.execute(sql, args).lastrowid


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def pack(vector):
    validate_vectors([vector], 1)
    blob = struct.pack(f'<{len(vector)}f', *vector)
    # Reject values that overflow/underflow in the stored float32 representation.
    validate_vectors([unpack(blob)], 1)
    return blob


def validate_vectors(vectors, count):
    from .llm.runtime import vector_dimension
    dimension=vector_dimension()
    if len(vectors) != count:
        raise ValueError('向量结果数量与输入不一致')
    for vector in vectors:
        if len(vector) != dimension:
            raise ValueError('向量维度与配置不一致')
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
            raise ValueError('向量包含无效或非有限数值')
        norm = math.sqrt(sum(x*x for x in vector))
        if not math.isfinite(norm) or norm < 1e-12:
            raise ValueError('向量为空或数值异常')


def unpack(blob):
    return list(struct.unpack(f'<{len(blob)//4}f', blob)) if blob else []


def set_paper_vector(paper_id, vector):
    blob = pack(vector)
    paper = one('SELECT id,title,abstract FROM papers WHERE id=?', (paper_id,))
    if not paper:
        return False
    from .vector_store import set_many
    return bool(set_many([paper], [blob]))
