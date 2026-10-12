"""Archive usage atomically, keeping API and subscription statistics additive."""
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings

SCHEMA = '''
CREATE TABLE IF NOT EXISTS llm_usage_daily (
 day TEXT NOT NULL,utc_day TEXT NOT NULL,model TEXT NOT NULL,feature TEXT NOT NULL,
 connection_id TEXT NOT NULL,billing_source TEXT NOT NULL,
 input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,
 unknown_usage INTEGER NOT NULL,calls INTEGER NOT NULL,
 PRIMARY KEY(day,utc_day,model,feature,connection_id,billing_source));
CREATE INDEX IF NOT EXISTS idx_usage_daily_utc ON llm_usage_daily(utc_day);
'''


def archive(db, records):
    """The caller deletes these exact raw IDs in the same transaction."""
    totals = defaultdict(lambda: [0, 0, 0, 0])
    zone = ZoneInfo(settings().tz)
    for row in records:
        stamp = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        key = (stamp.astimezone(zone).date().isoformat(), stamp.astimezone(timezone.utc).date().isoformat(),
               row['model'] or '', row['feature'] or '', row['connection_id'] or '', row['billing_source'])
        value = totals[key]
        value[0] += row['input_tokens'] or 0
        value[1] += row['output_tokens'] or 0
        value[2] += int(not row['usage_known'])
        value[3] += 1
    db.executemany('''INSERT INTO llm_usage_daily VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(day,utc_day,model,feature,connection_id,billing_source) DO UPDATE SET
        input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,
        unknown_usage=unknown_usage+excluded.unknown_usage,calls=calls+excluded.calls''',
        [(*key, *value) for key, value in totals.items()])


def usage_rows(db, since, local_start=None):
    """One SQLite snapshot reads raw and archived rows, without gaps or duplicates."""
    day = 'local_day(created_at)' if local_start else 'substr(created_at,1,10)'
    summary_day = 'day' if local_start else 'utc_day'
    return [dict(row) for row in db.execute(f'''SELECT day,model,feature,connection_id,billing_source,
        SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,
        SUM(unknown_usage) unknown_usage,SUM(calls) calls FROM (
        SELECT {day} day,model,feature,connection_id,billing_source,
            input_tokens,output_tokens,usage_known=0 unknown_usage,1 calls
        FROM llm_usage WHERE created_at>=?
        UNION ALL
        SELECT {summary_day} day,NULLIF(model,''),NULLIF(feature,''),NULLIF(connection_id,''),billing_source,
            input_tokens,output_tokens,unknown_usage,calls
        FROM llm_usage_daily WHERE {summary_day}>=?)
        GROUP BY day,model,feature,connection_id,billing_source''', (since, local_start or since[:10]))]


def breakdown(records, field, billing_source='api'):
    totals = {}
    for row in records:
        if row['billing_source'] != billing_source:
            continue
        value = totals.setdefault(row[field], {field: row[field], **{k: 0 for k in
            ('input_tokens', 'output_tokens', 'unknown_usage', 'calls')}})
        for key in ('input_tokens', 'output_tokens', 'unknown_usage', 'calls'):
            value[key] += row[key] or 0
    return sorted(totals.values(), key=lambda r: -(r['input_tokens']+r['output_tokens']))
