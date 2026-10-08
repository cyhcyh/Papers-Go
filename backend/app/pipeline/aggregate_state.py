"""Dirty dates/topics for scheduled aggregates, including late edits and undo."""
from datetime import date,datetime,time,timezone
from zoneinfo import ZoneInfo
from ..config import settings,now


def metric_queue(prefix):
    return f'''INSERT INTO metric_dirty(user_id,utc_date,revision)
      SELECT {prefix}.user_id,substr({prefix}.created_at,1,10),1 WHERE EXISTS(SELECT 1 FROM users WHERE id={prefix}.user_id)
      ON CONFLICT(user_id,utc_date) DO UPDATE SET revision=revision+1;
      INSERT INTO metric_dirty(user_id,utc_date,revision)
      SELECT user_id,substr(created_at,1,10),1 FROM interactions WHERE id={prefix}.target_id AND {prefix}.action='undo'
      ON CONFLICT(user_id,utc_date) DO UPDATE SET revision=revision+1;'''


def topic_queue(prefix):
    return f'''INSERT INTO trend_dirty(date,topic_id,revision)
      SELECT COALESCE(p.published,p.ingested_date),{prefix}.topic_id,1 FROM papers p
      WHERE p.id={prefix}.paper_id AND EXISTS(SELECT 1 FROM topics WHERE id={prefix}.topic_id)
      ON CONFLICT(date,topic_id) DO UPDATE SET revision=revision+1;'''


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS metric_dirty(
      user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,utc_date TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY(user_id,utc_date));
      CREATE TABLE IF NOT EXISTS trend_dirty(
      date TEXT NOT NULL,topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,revision INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY(date,topic_id));
      CREATE INDEX IF NOT EXISTS idx_interactions_undo ON interactions(target_id) WHERE action='undo';''')
    for operation in ('INSERT','DELETE','UPDATE'):
        prefixes=('OLD','NEW') if operation=='UPDATE' else ('NEW',) if operation=='INSERT' else ('OLD',)
        db.executescript(f'CREATE TRIGGER IF NOT EXISTS metric_{operation.lower()} AFTER {operation} ON interactions BEGIN '+''.join(metric_queue(p) for p in prefixes)+' END;')
        db.executescript(f'CREATE TRIGGER IF NOT EXISTS trend_link_{operation.lower()} AFTER {operation} ON paper_topics BEGIN '+''.join(topic_queue(p) for p in prefixes)+' END;')
    db.executescript('''DROP TRIGGER IF EXISTS trend_paper_update;
      CREATE TRIGGER trend_paper_update AFTER UPDATE OF published,ingested_date,quality_score,scored ON papers
      WHEN NEW.published IS NOT OLD.published OR NEW.ingested_date IS NOT OLD.ingested_date OR NEW.quality_score IS NOT OLD.quality_score OR NEW.scored IS NOT OLD.scored BEGIN
      INSERT INTO trend_dirty(date,topic_id,revision) SELECT COALESCE(OLD.published,OLD.ingested_date),topic_id,1 FROM paper_topics WHERE paper_id=NEW.id
      ON CONFLICT(date,topic_id) DO UPDATE SET revision=revision+1;
      INSERT INTO trend_dirty(date,topic_id,revision) SELECT COALESCE(NEW.published,NEW.ingested_date),topic_id,1 FROM paper_topics WHERE paper_id=NEW.id
      ON CONFLICT(date,topic_id) DO UPDATE SET revision=revision+1;
      END;
      CREATE TRIGGER IF NOT EXISTS trend_paper_delete BEFORE DELETE ON papers BEGIN
      INSERT INTO trend_dirty(date,topic_id,revision) SELECT COALESCE(OLD.published,OLD.ingested_date),topic_id,1 FROM paper_topics WHERE paper_id=OLD.id
      ON CONFLICT(date,topic_id) DO UPDATE SET revision=revision+1; END;''')
    if not db.execute("SELECT 1 FROM app_migrations WHERE name='incremental_aggregates_v1'").fetchone():
        db.execute('INSERT OR IGNORE INTO metric_dirty(user_id,utc_date) SELECT user_id,substr(created_at,1,10) FROM interactions GROUP BY user_id,substr(created_at,1,10)')
        for r in db.execute('SELECT date,user_id FROM daily_metrics').fetchall():
            utc=datetime.combine(date.fromisoformat(r['date']),time.min,ZoneInfo(settings().tz)).astimezone(timezone.utc).date().isoformat()
            db.execute('INSERT OR IGNORE INTO metric_dirty(user_id,utc_date) VALUES(?,?)',(r['user_id'],utc))
        db.execute('INSERT OR IGNORE INTO trend_dirty(date,topic_id) SELECT COALESCE(p.published,p.ingested_date),pt.topic_id FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id GROUP BY COALESCE(p.published,p.ingested_date),pt.topic_id')
        db.execute('INSERT OR IGNORE INTO trend_dirty(date,topic_id) SELECT date,topic_id FROM topic_daily_stats')
        db.execute("INSERT INTO app_migrations VALUES('incremental_aggregates_v1',?)",(now(),))
