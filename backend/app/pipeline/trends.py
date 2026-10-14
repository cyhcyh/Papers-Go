from ..llm import runtime as models
import json
from datetime import date, timedelta
from ..config import today, now
from ..db import connect, rows, one, execute, dumps
from ..interest.profile import current
from .direction_trends import trend_summary, ensure_direction_trend, cached
from ..logs import event


def trend_stats():
    from ..retention import STATS_DAYS
    cutoff=(date.fromisoformat(today())-timedelta(days=STATS_DAYS)).isoformat()
    for item in rows('SELECT * FROM trend_dirty ORDER BY date,topic_id LIMIT 500'):
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if item['date']<cutoff:
                db.execute('DELETE FROM trend_dirty WHERE date=? AND topic_id=? AND revision=?',(item['date'],item['topic_id'],item['revision']))
                continue
            result=db.execute('SELECT COUNT(*) n,AVG(CASE WHEN p.scored=1 THEN p.quality_score END) quality FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id WHERE pt.topic_id=? AND COALESCE(p.published,p.ingested_date)=?',(item['topic_id'],item['date'])).fetchone()
            if result['n']:
                db.execute('INSERT INTO topic_daily_stats VALUES(?,?,?,?) ON CONFLICT(date,topic_id) DO UPDATE SET paper_count=excluded.paper_count,avg_quality=excluded.avg_quality',(item['date'],item['topic_id'],result['n'],result['quality']))
            else:db.execute('DELETE FROM topic_daily_stats WHERE date=? AND topic_id=?',(item['date'],item['topic_id']))
            db.execute('DELETE FROM trend_dirty WHERE date=? AND topic_id=? AND revision=?',(item['date'],item['topic_id'],item['revision']))


def author_movements(user_id, *, offset=0, limit=5, count_total=True):
    """Paginate the existing exact-name, seven-day author scope without model calls."""
    from ..paper_index import ready
    if ready():
        followed='SELECT DISTINCT n.name FROM user_paper_state s JOIN paper_names n ON n.paper_id=s.paper_id WHERE s.user_id=? AND s.saved=1'
        membership='p.id IN (SELECT paper_id FROM paper_names WHERE name IN ('+followed+'))'
    else:
        followed='SELECT DISTINCT a.value AS name FROM user_paper_state s JOIN papers saved ON saved.id=s.paper_id JOIN json_each(saved.authors) a WHERE s.user_id=? AND s.saved=1'
        membership='EXISTS(SELECT 1 FROM json_each(p.authors) a WHERE a.value IN ('+followed+'))'
    cutoff=(date.fromisoformat(today())-timedelta(days=6)).isoformat()
    where=membership+' AND p.published>=? AND p.published<=?'
    args=(user_id,cutoff,today())
    with connect() as db:
        db.execute('BEGIN')
        authors={r['name'] for r in db.execute(followed,(user_id,))}
        if not authors:return {'items':[],'total':0,'offset':offset,'limit':limit}
        total=db.execute('SELECT COUNT(*) n FROM papers p WHERE '+where,args).fetchone()['n'] if count_total else 0
        candidates=db.execute('SELECT p.id,p.title,p.authors,p.published,p.primary_category,p.venue FROM papers p WHERE '+where+' ORDER BY p.published DESC,p.id DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        items=[{**dict(p),'authors':sorted(authors.intersection(json.loads(p['authors'])))} for p in candidates]
    return {'items':items,'total':total,'offset':offset,'limit':limit}


def trend_data(user_id, window='day', *, include_movements=True):
    from .direction_trends import scoped_papers, coverage, trend_context, topic_distribution
    from .arxiv_daily import latest
    context=trend_context(user_id);batch=context['batch'];papers=scoped_papers(user_id,context=context);info=coverage(user_id,context=context,papers=papers)
    ids=[p['id'] for p in papers]
    topics=[];movements=[]
    if ids:
        topics=topic_distribution(ids,minimum=3,limit=6)
        for topic in topics:topic['share']=round(topic['count']/len(ids)*100,1)
    # Author updates retain their independent seven-day scope, including papers
    # outside today's announcement and outside the selected research directions.
    if include_movements:
        movements=[{key:p[key] for key in ('id','title','authors','published')} for p in author_movements(user_id,limit=30,count_total=False)['items']]
    direction=trend_summary(user_id,context=context,papers=papers)
    report={'content':direction['summary'],'created_at':direction['created_at'],'items':direction['items'],'period':direction['period'],'papers':direction['papers']} if direction['items'] else None
    return {'topics':topics,'movements':movements[:30],'report':report,'status':direction['status'],'window':'day','coverage':info}


async def trend_report():
    # One shared result per announcement/scope, with no weekly archives/notices.
    completed=0;skipped=0;failed={}
    cutoff=(date.fromisoformat(today())-timedelta(days=14)).isoformat()
    requested={r['user_id']:r['requested_at'] for r in rows('SELECT * FROM arxiv_trend_requests')}
    users=rows('SELECT u.id FROM users u WHERE disabled=0 AND (EXISTS(SELECT 1 FROM arxiv_trend_requests r WHERE r.user_id=u.id) OR EXISTS(SELECT 1 FROM interactions i WHERE i.user_id=u.id AND i.created_at>=?) OR EXISTS(SELECT 1 FROM chat_sessions c WHERE c.user_id=u.id AND c.created_at>=?) OR EXISTS(SELECT 1 FROM interest_profile p WHERE p.user_id=u.id AND p.created_at>=?))',(cutoff,cutoff,cutoff))
    for user in users:
        if await ensure_direction_trend(user['id']):completed+=1
        else:
            value=cached(user['id'])
            if value and value['error']:failed[value['audience']]=value['error']
            else:skipped+=1
        if user['id'] in requested:
            execute('DELETE FROM arxiv_trend_requests WHERE user_id=? AND requested_at=?',(user['id'],requested[user['id']]))
    event('trend','趋势整理结果',job='trend_report',level='error' if failed else 'info',completed=completed,skipped=skipped,failed=len(failed))
    if failed:raise ValueError(f'本轮完成 {completed} 份趋势，{len(failed)} 份未完成。'+next(iter(failed.values())))
    return completed
