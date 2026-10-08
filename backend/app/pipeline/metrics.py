from .. import prompts
from ..llm import runtime as models
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ..config import today, settings
from ..db import connect, rows, execute, dumps
from ..llm.provider import cloud
from ..taxonomy import scoped_topics


def metrics():
    zone=ZoneInfo(settings().tz)
    for item in rows('SELECT * FROM metric_dirty ORDER BY utc_date,user_id LIMIT 500'):
        utc=datetime.fromisoformat(item['utc_date']).replace(tzinfo=timezone.utc)
        days={utc.astimezone(zone).date(),(utc+timedelta(days=1,microseconds=-1)).astimezone(zone).date()}
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for day in days:
                start=datetime.combine(day,datetime.min.time(),zone).astimezone(timezone.utc).isoformat()
                end=datetime.combine(day+timedelta(days=1),datetime.min.time(),zone).astimezone(timezone.utc).isoformat()
                events=db.execute("SELECT paper_id,action,view_rule,dwell_ms FROM interactions i WHERE user_id=? AND created_at>=? AND created_at<? AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo')",(item['user_id'],start,end)).fetchall()
                if not events:
                    db.execute('DELETE FROM daily_metrics WHERE date=? AND user_id=?',(day.isoformat(),item['user_id']));continue
                counts={'skip':0,'like':0,'save':0,'expand':0,'quick':0};shown=set();considered=set()
                for e in events:
                    if e['action'] in ('view','skip','like','save'):considered.add(e['paper_id'])
                    if e['action']=='view' or e['view_rule']==0 and e['action'] in ('skip','like','save'):shown.add(e['paper_id'])
                    if e['action'] in counts:
                        counts[e['action']]+=1
                        if e['action']=='skip' and e['dwell_ms']<1500:counts['quick']+=1
                denominator=max(1,len(considered))
                db.execute('INSERT INTO daily_metrics(date,user_id,shown,skipped,liked,saved,expanded,like_rate,quick_skip_rate,considered) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,user_id) DO UPDATE SET shown=excluded.shown,skipped=excluded.skipped,liked=excluded.liked,saved=excluded.saved,expanded=excluded.expanded,like_rate=excluded.like_rate,quick_skip_rate=excluded.quick_skip_rate,considered=excluded.considered',
                           (day.isoformat(),item['user_id'],len(shown),counts['skip'],counts['like'],counts['save'],counts['expand'],counts['like']/denominator,counts['quick']/denominator,len(considered)))
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
