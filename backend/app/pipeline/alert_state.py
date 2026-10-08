"""Small, persistent dirty queues; no per-user/per-paper matrix or model calls."""
from datetime import date, timedelta
from ..config import now, today


def paper_queue(identifier):
    return f'''INSERT INTO alert_paper_state(paper_id,revision,pending)
        SELECT {identifier},1,1 WHERE EXISTS(SELECT 1 FROM papers WHERE id={identifier})
        ON CONFLICT(paper_id) DO UPDATE SET revision=revision+1,pending=1,cursor_user=0;'''


def user_queue(identifier):
    return f'''INSERT INTO alert_user_state(user_id,revision,pending)
        SELECT {identifier},1,1 WHERE EXISTS(SELECT 1 FROM users WHERE id={identifier})
        ON CONFLICT(user_id) DO UPDATE SET revision=revision+1,pending=1,
        cursor_date='',cursor_id=0,window_start='';'''


def initialize(db):
    if 'cursor_user' not in {r['name'] for r in db.execute('PRAGMA table_info(alert_paper_state)')}:
        db.execute('ALTER TABLE alert_paper_state ADD COLUMN cursor_user INTEGER NOT NULL DEFAULT 0')
    columns={r['name'] for r in db.execute('PRAGMA table_info(watches)')}
    for name,definition in [('topic_id','INTEGER REFERENCES topics(id) ON DELETE SET NULL'),('topic_key','TEXT'),('author_id','TEXT')]:
        if name not in columns:db.execute('ALTER TABLE watches ADD COLUMN '+name+' '+definition)
    db.execute('CREATE INDEX IF NOT EXISTS idx_alerts_recent ON papers(ingested_date,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_watches_active ON watches(user_id,active,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_state_saved ON user_paper_state(user_id,paper_id) WHERE saved=1')
    db.execute('CREATE INDEX IF NOT EXISTS idx_author_metrics_name ON author_metrics(name COLLATE NOCASE)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_author_links_name ON paper_author_links(name COLLATE NOCASE,author_id)')
    db.execute('DROP TRIGGER IF EXISTS alert_paper_update')
    db.executescript('''CREATE TRIGGER IF NOT EXISTS alert_paper_insert AFTER INSERT ON papers BEGIN '''+paper_queue('NEW.id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_paper_update AFTER UPDATE OF title,abstract,authors,arxiv_id,embedding,venue,primary_category,categories,ingested_date ON papers
        WHEN NEW.title IS NOT OLD.title OR NEW.abstract IS NOT OLD.abstract OR NEW.authors IS NOT OLD.authors
          OR NEW.arxiv_id IS NOT OLD.arxiv_id OR (NEW.embedding IS NOT OLD.embedding AND (NEW.embedding IS NOT X'01' OR OLD.embedding IS NULL)) OR NEW.venue IS NOT OLD.venue
          OR NEW.primary_category IS NOT OLD.primary_category OR NEW.categories IS NOT OLD.categories OR NEW.ingested_date IS NOT OLD.ingested_date
        BEGIN '''+paper_queue('NEW.id')+''' END;''')
    for table in ('paper_topics','paper_author_links'):
        db.executescript(f'''CREATE TRIGGER IF NOT EXISTS alert_{table}_insert AFTER INSERT ON {table} BEGIN {paper_queue('NEW.paper_id')} END;
            CREATE TRIGGER IF NOT EXISTS alert_{table}_delete AFTER DELETE ON {table} BEGIN {paper_queue('OLD.paper_id')} END;
            CREATE TRIGGER IF NOT EXISTS alert_{table}_update AFTER UPDATE ON {table} BEGIN {paper_queue('OLD.paper_id')}{paper_queue('NEW.paper_id')} END;''')
    db.executescript('''CREATE TRIGGER IF NOT EXISTS alert_reading_insert AFTER INSERT ON reading_cards WHEN NEW.status='ready'
        BEGIN '''+paper_queue('NEW.paper_id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_reading_update AFTER UPDATE OF status,card_json ON reading_cards
        WHEN NEW.status='ready' AND (NEW.status IS NOT OLD.status OR NEW.card_json IS NOT OLD.card_json)
        BEGIN '''+paper_queue('NEW.paper_id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_reading_delete AFTER DELETE ON reading_cards WHEN OLD.status='ready'
        BEGIN '''+paper_queue('OLD.paper_id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_topic_update AFTER UPDATE OF name_zh,name_en,status ON topics
        WHEN NEW.name_zh IS NOT OLD.name_zh OR NEW.name_en IS NOT OLD.name_en OR NEW.status IS NOT OLD.status BEGIN
        INSERT INTO alert_paper_state(paper_id,revision,pending)
        SELECT paper_id,1,1 FROM paper_topics WHERE topic_id=NEW.id
        ON CONFLICT(paper_id) DO UPDATE SET revision=revision+1,pending=1,cursor_user=0; END;
        CREATE TRIGGER IF NOT EXISTS alert_profile_insert AFTER INSERT ON interest_profile
        WHEN INSTR(NEW.content,'在研方向')>0 OR EXISTS(
          SELECT 1 FROM interest_profile WHERE user_id=NEW.user_id AND id<>NEW.id
          AND version<NEW.version AND INSTR(content,'在研方向')>0
          AND version=(SELECT MAX(version) FROM interest_profile WHERE user_id=NEW.user_id AND version<NEW.version))
        BEGIN '''+user_queue('NEW.user_id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_profile_update AFTER UPDATE OF content,structured,embedding ON interest_profile
        WHEN (INSTR(NEW.content,'在研方向')>0 OR INSTR(OLD.content,'在研方向')>0)
          AND (NEW.content IS NOT OLD.content OR NEW.structured IS NOT OLD.structured OR NEW.embedding IS NOT OLD.embedding)
        BEGIN '''+user_queue('NEW.user_id')+''' END;''')
    for operation in ('INSERT','DELETE'):
        prefix='NEW' if operation=='INSERT' else 'OLD'
        db.executescript(f'''CREATE TRIGGER IF NOT EXISTS alert_watch_{operation.lower()} AFTER {operation} ON watches
            WHEN {prefix}.active=1 BEGIN {user_queue(prefix+'.user_id')} END;
            CREATE TRIGGER IF NOT EXISTS alert_saved_{operation.lower()} AFTER {operation} ON user_paper_state
            WHEN {prefix}.saved=1 BEGIN {user_queue(prefix+'.user_id')} END;''')
    db.executescript('''CREATE TRIGGER IF NOT EXISTS alert_watch_update AFTER UPDATE OF type,value,active ON watches
        WHEN NEW.type IS NOT OLD.type OR NEW.value IS NOT OLD.value OR NEW.active IS NOT OLD.active
        BEGIN UPDATE watches SET topic_id=NULL,topic_key=NULL,author_id=NULL
        WHERE id=NEW.id AND (NEW.type IS NOT OLD.type OR NEW.value IS NOT OLD.value);
        '''+user_queue('NEW.user_id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_saved_update AFTER UPDATE OF saved ON user_paper_state WHEN NEW.saved IS NOT OLD.saved
        BEGIN '''+user_queue('NEW.user_id')+''' END;
        CREATE TRIGGER IF NOT EXISTS alert_user_update AFTER UPDATE OF notifications_enabled,disabled ON users
        WHEN NEW.notifications_enabled IS NOT OLD.notifications_enabled OR NEW.disabled IS NOT OLD.disabled
        BEGIN '''+user_queue('NEW.id')+''' END;''')
    db.executescript('''CREATE TRIGGER IF NOT EXISTS alert_baseline_update AFTER UPDATE OF title,arxiv_id ON papers
        WHEN NEW.title IS NOT OLD.title OR NEW.arxiv_id IS NOT OLD.arxiv_id BEGIN
        INSERT INTO alert_user_state(user_id,revision,pending)
        SELECT user_id,1,1 FROM user_paper_state WHERE paper_id=NEW.id AND saved=1
        ON CONFLICT(user_id) DO UPDATE SET revision=revision+1,pending=1,cursor_date='',cursor_id=0,window_start=''; END;''')
    if not db.execute("SELECT 1 FROM app_migrations WHERE name='incremental_alerts_v1'").fetchone():
        cutoff=(date.fromisoformat(today())-timedelta(days=1)).isoformat()
        db.execute('INSERT OR IGNORE INTO alert_paper_state(paper_id) SELECT id FROM papers WHERE ingested_date>=?',(cutoff,))
        # The initial paper sweep already checks every eligible user; do not duplicate it with personal sweeps.
        db.execute('INSERT OR IGNORE INTO alert_user_state(user_id,pending) SELECT id,0 FROM users WHERE disabled=0 AND notifications_enabled=1')
        db.execute("INSERT INTO app_migrations VALUES('incremental_alerts_v1',?)",(now(),))
