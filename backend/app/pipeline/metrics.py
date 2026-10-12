from .. import prompts
from ..llm import runtime as models
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ..config import today, settings
from ..db import connect, rows, execute, dumps
from ..llm.provider import cloud
from ..taxonomy import scoped_topics


def aggregate_day(db, user_id, day, archived=False):
    frozen=db.execute('SELECT archived FROM daily_metrics WHERE date=? AND user_id=?',(day.isoformat(),user_id)).fetchone()
    if frozen and frozen['archived']:return
    zone=ZoneInfo(settings().tz)
    start=datetime.combine(day,datetime.min.time(),zone).astimezone(timezone.utc).isoformat()
    end=datetime.combine(day+timedelta(days=1),datetime.min.time(),zone).astimezone(timezone.utc).isoformat()
    counts=db.execute("""SELECT COUNT(*) n,
        COUNT(DISTINCT CASE WHEN action='view' OR (view_rule=0 AND action IN ('skip','like','save')) THEN paper_id END) shown,
        COUNT(DISTINCT CASE WHEN action IN ('view','skip','like','save') THEN paper_id END) considered,
        SUM(action='skip') skipped,SUM(action='like') liked,SUM(action='save') saved,SUM(action='expand') expanded,
        SUM(action='skip' AND dwell_ms<1500) quick
        FROM interactions i WHERE user_id=? AND created_at>=? AND created_at<?
        AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo')""",(user_id,start,end)).fetchone()
    if not counts['n']:
        # Only discard an unfinished aggregate when every original event was
        # undone. Previously frozen totals are retained even without raw events.
        db.execute('DELETE FROM daily_metrics WHERE date=? AND user_id=? AND archived=0',(day.isoformat(),user_id))
        return
    denominator=max(1,counts['considered'])
    db.execute('INSERT INTO daily_metrics(date,user_id,shown,skipped,liked,saved,expanded,like_rate,quick_skip_rate,considered,archived) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,user_id) DO UPDATE SET shown=excluded.shown,skipped=excluded.skipped,liked=excluded.liked,saved=excluded.saved,expanded=excluded.expanded,like_rate=excluded.like_rate,quick_skip_rate=excluded.quick_skip_rate,considered=excluded.considered,archived=excluded.archived',
               (day.isoformat(),user_id,counts['shown'],counts['skipped'],counts['liked'],counts['saved'],counts['expanded'],counts['liked']/denominator,counts['quick']/denominator,counts['considered'],int(archived)))


def metrics():
    from ..retention import STATS_DAYS
    cutoff=date.fromisoformat(today())-timedelta(days=STATS_DAYS)
    zone=ZoneInfo(settings().tz)
    for item in rows('SELECT * FROM metric_dirty ORDER BY utc_date,user_id LIMIT 500'):
        utc=datetime.fromisoformat(item['utc_date']).replace(tzinfo=timezone.utc)
        days={utc.astimezone(zone).date(),(utc+timedelta(days=1,microseconds=-1)).astimezone(zone).date()}
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for day in days:
                frozen=db.execute('SELECT archived FROM daily_metrics WHERE date=? AND user_id=?',(day.isoformat(),item['user_id'])).fetchone()
                if day>=cutoff and not (frozen and frozen['archived']):aggregate_day(db,item['user_id'],day)
            db.execute('DELETE FROM metric_dirty WHERE user_id=? AND utc_date=? AND revision=?',(item['user_id'],item['utc_date'],item['revision']))


async def audit():
    from ..task_settings import configuration
    limit = configuration()['advanced']['audit']['sample_size']
    # Sample IDs first so the join/aggregation only touches the bounded sample.
    papers = rows('SELECT p.id,p.title,p.abstract,p.primary_category,p.categories,p.venue,GROUP_CONCAT(t.name_en) AS topics FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id JOIN topics t ON t.id=pt.topic_id WHERE p.id IN (SELECT paper_id FROM paper_topics GROUP BY paper_id ORDER BY RANDOM() LIMIT ?) GROUP BY p.id', (limit,))
    if not papers:
        return
    topics = rows("SELECT id,name_en,category_keys FROM topics WHERE status='active'")
    for paper in papers:
        paper['candidate_topics'] = scoped_topics(paper, topics)
    data = await models.complete('audit', [{'role':'system','content':prompts.get('audit')},
                                {'role':'user','content':dumps(papers)}],json_mode=True,purpose='fast')
    checks = data.get('checks',[])
    valid_ids = {p['id'] for p in papers}
    checks = [c for c in checks if c.get('id') in valid_ids]
    result = {'samples':len(checks),'accuracy':sum(bool(c.get('correct')) for c in checks)/max(1,len(checks))}
    # Only repair uncategorized entries, retaining reviewed classifications in other cases.
    for check in checks:
        is_uncategorized = next((p for p in papers if p['id']==check['id'] and p['topics']=='Uncategorized'),None)
        allowed = {t['id'] for t in is_uncategorized['candidate_topics']} if is_uncategorized else set()
        suggested = [i for i in check.get('suggested_topic_ids',[]) if i in allowed][:1]
        if is_uncategorized and suggested:
            with connect() as db:
                db.execute('DELETE FROM paper_topics WHERE paper_id=?',(check['id'],))
                for tid in suggested:
                    db.execute('INSERT INTO paper_topics VALUES(?,?,?)',(check['id'],tid,.8))
    from ..config import now
    execute('INSERT INTO audit_log(action,detail,created_at) VALUES(?,?,?)',('classification.audit',dumps(result),now()))
