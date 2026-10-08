"""Shared paper lifetimes and social counters; no model or cache policy involved."""
from datetime import datetime, timedelta, timezone

from .config import now

INITIAL_DAYS = 365
SOCIAL_DAYS = 730
REPEAT_LIMIT = 5
MIGRATION = 'paper-lifetime-social-v1'


def expiry_after(stamp, days):
    value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value.astimezone(timezone.utc) + timedelta(days=days)).isoformat(timespec='seconds')


def initialize(db):
    for table, fields in (
        ('papers', [('expires_at', 'TEXT'), ('like_count', 'INTEGER NOT NULL DEFAULT 0 CHECK(like_count>=0)'),
                    ('save_count', 'INTEGER NOT NULL DEFAULT 0 CHECK(save_count>=0)')]),
        ('user_paper_state', [('liked_ever', 'INTEGER NOT NULL DEFAULT 0'),
                              ('saved_ever', 'INTEGER NOT NULL DEFAULT 0'),
                              ('life_renewals', 'INTEGER NOT NULL DEFAULT 0 CHECK(life_renewals BETWEEN 0 AND 5)')]),
    ):
        columns = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        for name, definition in fields:
            if name not in columns:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + definition)
    db.execute('CREATE INDEX IF NOT EXISTS idx_papers_expiry ON papers(expires_at,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_state_paper ON user_paper_state(paper_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_interactions_paper ON interactions(paper_id,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_interactions_target ON interactions(target_id) WHERE target_id IS NOT NULL')
    db.execute('CREATE INDEX IF NOT EXISTS idx_notifications_paper ON notifications(paper_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_redo_paper ON pipeline_redo_items(paper_id)')
    db.execute('CREATE TABLE IF NOT EXISTS retired_paper_sources(source_id TEXT PRIMARY KEY,retired_at TEXT NOT NULL)')
    if not db.execute('SELECT 1 FROM app_migrations WHERE name=?', (MIGRATION,)).fetchone():
        db.execute("""UPDATE papers SET expires_at=COALESCE(
            strftime('%Y-%m-%dT%H:%M:%S+00:00',created_at,'+365 days'),
            strftime('%Y-%m-%dT%H:%M:%S+00:00',ingested_date,'+365 days')) WHERE expires_at IS NULL""")
        # Preserve historical lifetimes and consume the same per-user repeat budget.
        history = {}
        for row in db.execute("""SELECT i.user_id,i.paper_id,i.action,i.created_at FROM interactions i
            JOIN papers p ON p.id=i.paper_id JOIN users u ON u.id=i.user_id
            WHERE i.action IN ('like','save') ORDER BY i.id"""):
            key = row['user_id'], row['paper_id']
            entry = history.setdefault(key, {'liked_ever': 0, 'saved_ever': 0, 'life_renewals': 0})
            field = 'liked_ever' if row['action'] == 'like' else 'saved_ever'
            allowed = not entry[field] or entry['life_renewals'] < REPEAT_LIMIT
            if entry[field] and allowed:
                entry['life_renewals'] += 1
            entry[field] = 1
            if allowed:
                expiry = expiry_after(row['created_at'], SOCIAL_DAYS)
                entry['expiry'] = max(entry.get('expiry', expiry), expiry)
        for (uid, pid), entry in history.items():
            db.execute('''INSERT INTO user_paper_state(user_id,paper_id,liked_ever,saved_ever,life_renewals)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id,paper_id) DO UPDATE SET
                liked_ever=excluded.liked_ever,saved_ever=excluded.saved_ever,life_renewals=excluded.life_renewals''',
                (uid, pid, entry['liked_ever'], entry['saved_ever'], entry['life_renewals']))
            db.execute('UPDATE papers SET expires_at=MAX(expires_at,?) WHERE id=?', (entry['expiry'], pid))
        # Legacy active states without an interaction log still receive their benefit.
        for row in db.execute('''SELECT * FROM user_paper_state
            WHERE (liked=1 AND liked_ever=0) OR (saved=1 AND saved_ever=0)''').fetchall():
            if row['updated_at']:
                db.execute('UPDATE papers SET expires_at=MAX(expires_at,?) WHERE id=?',
                           (expiry_after(row['updated_at'], SOCIAL_DAYS), row['paper_id']))
        db.execute('UPDATE user_paper_state SET liked_ever=MAX(liked_ever,liked),saved_ever=MAX(saved_ever,saved)')
        db.execute('''UPDATE papers SET
            like_count=COALESCE((SELECT SUM(liked) FROM user_paper_state WHERE paper_id=papers.id),0),
            save_count=COALESCE((SELECT SUM(saved) FROM user_paper_state WHERE paper_id=papers.id),0)''')
        db.execute('INSERT INTO app_migrations VALUES(?,?)', (MIGRATION, now()))
    trigger_migration = 'paper-lifetime-config-trigger-v1'
    if not db.execute('SELECT 1 FROM app_migrations WHERE name=?', (trigger_migration,)).fetchone():
        db.execute('DROP TRIGGER IF EXISTS paper_lifetime_insert')
        db.execute('INSERT INTO app_migrations VALUES(?,?)', (trigger_migration, now()))
    db.execute("""CREATE TRIGGER IF NOT EXISTS paper_lifetime_insert AFTER INSERT ON papers
        WHEN NEW.expires_at IS NULL BEGIN UPDATE papers SET expires_at=COALESCE(
        strftime('%Y-%m-%dT%H:%M:%S+00:00',NEW.created_at,'+'||COALESCE((SELECT value FROM app_settings WHERE name='paper_initial_days'),365)||' days'),
        strftime('%Y-%m-%dT%H:%M:%S+00:00',NEW.ingested_date,'+'||COALESCE((SELECT value FROM app_settings WHERE name='paper_initial_days'),365)||' days'),
        strftime('%Y-%m-%dT%H:%M:%S+00:00','now','+'||COALESCE((SELECT value FROM app_settings WHERE name='paper_initial_days'),365)||' days')) WHERE id=NEW.id; END""")
    db.execute('''CREATE TRIGGER IF NOT EXISTS paper_social_insert AFTER INSERT ON user_paper_state
        WHEN NEW.liked!=0 OR NEW.saved!=0 BEGIN
        UPDATE papers SET like_count=like_count+NEW.liked,save_count=save_count+NEW.saved WHERE id=NEW.paper_id; END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS paper_social_update AFTER UPDATE OF liked,saved ON user_paper_state
        WHEN NEW.liked!=OLD.liked OR NEW.saved!=OLD.saved BEGIN
        UPDATE papers SET like_count=like_count+NEW.liked-OLD.liked,
        save_count=save_count+NEW.saved-OLD.saved WHERE id=NEW.paper_id; END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS paper_social_delete AFTER DELETE ON user_paper_state
        WHEN OLD.liked!=0 OR OLD.saved!=0 BEGIN
        UPDATE papers SET like_count=like_count-OLD.liked,save_count=save_count-OLD.saved WHERE id=OLD.paper_id; END''')


def extend_life(db, user_id, paper_id, action, stamp):
    """Call only on a real 0 -> 1 transition, in the interaction transaction."""
    field = 'liked_ever' if action == 'like' else 'saved_ever'
    state = db.execute('SELECT liked_ever,saved_ever,life_renewals FROM user_paper_state WHERE user_id=? AND paper_id=?',
                       (user_id, paper_id)).fetchone()
    repeat = bool(state[field])
    limits = {row['name']:int(row['value']) for row in db.execute("SELECT name,value FROM app_settings WHERE name IN ('paper_social_days','paper_repeat_limit')")}
    if repeat and state['life_renewals'] >= limits.get('paper_repeat_limit', REPEAT_LIMIT):
        return False
    db.execute('UPDATE user_paper_state SET ' + field + '=1,life_renewals=life_renewals+? WHERE user_id=? AND paper_id=?',
               (int(repeat), user_id, paper_id))
    db.execute('UPDATE papers SET expires_at=MAX(expires_at,?) WHERE id=?',
               (expiry_after(stamp, limits.get('paper_social_days', SOCIAL_DAYS)), paper_id))
    return True


def social_state(db, paper_id, user_id):
    row = db.execute('''SELECT p.like_count,p.save_count,p.expires_at,
        COALESCE(s.liked,0) liked,COALESCE(s.saved,0) saved FROM papers p
        LEFT JOIN user_paper_state s ON s.paper_id=p.id AND s.user_id=? WHERE p.id=?''',
        (user_id, paper_id)).fetchone()
    return {**dict(row), 'liked': bool(row['liked']), 'saved': bool(row['saved'])} if row else {}
