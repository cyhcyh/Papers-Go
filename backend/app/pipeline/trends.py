from ..llm import runtime as models
import json
from datetime import date, timedelta
from ..config import today, now
from ..db import connect, rows, one, execute, dumps
from ..interest.profile import current
from .direction_trends import trend_summary, ensure_direction_trend


def trend_stats():
    for item in rows('SELECT * FROM trend_dirty ORDER BY date,topic_id LIMIT 500'):
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result=db.execute('SELECT COUNT(*) n,AVG(CASE WHEN p.scored=1 THEN p.quality_score END) quality FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id WHERE pt.topic_id=? AND COALESCE(p.published,p.ingested_date)=?',(item['topic_id'],item['date'])).fetchone()
            if result['n']:
                db.execute('INSERT INTO topic_daily_stats VALUES(?,?,?,?) ON CONFLICT(date,topic_id) DO UPDATE SET paper_count=excluded.paper_count,avg_quality=excluded.avg_quality',(item['date'],item['topic_id'],result['n'],result['quality']))
            else:db.execute('DELETE FROM topic_daily_stats WHERE date=? AND topic_id=?',(item['date'],item['topic_id']))
            db.execute('DELETE FROM trend_dirty WHERE date=? AND topic_id=? AND revision=?',(item['date'],item['topic_id'],item['revision']))


def trend_data(user_id, window='day'):
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
    saved=rows('SELECT p.authors FROM papers p JOIN user_paper_state s ON s.paper_id=p.id WHERE s.user_id=? AND s.saved=1',(user_id,))
    authors={a for p in saved for a in json.loads(p['authors'])}
    if authors:
        from ..paper_index import ready
        placeholders=','.join('?' for _ in authors)
        membership='p.id IN (SELECT paper_id FROM paper_names WHERE name IN ('+placeholders+'))' if ready() else 'EXISTS(SELECT 1 FROM json_each(p.authors) a WHERE a.value IN ('+placeholders+'))'
        cutoff=(date.fromisoformat(today())-timedelta(days=6)).isoformat()
        candidates=rows('SELECT p.id,p.title,p.authors,p.published FROM papers p WHERE '+membership+' AND p.published>=? AND p.published<=? ORDER BY p.published DESC,p.id DESC LIMIT 30',[*sorted(authors),cutoff,today()])
        for p in candidates:
            names=authors.intersection(json.loads(p['authors']))
            if names:movements.append({**p,'authors':sorted(names)})
    direction=trend_summary(user_id,context=context,papers=papers)
    report={'content':direction['summary'],'created_at':direction['created_at'],'items':direction['items'],'period':direction['period']} if direction['items'] else None
    return {'topics':topics,'movements':movements[:30],'report':report,'status':direction['status'],'window':'day','coverage':info}


async def trend_report():
    # One shared result per announcement/scope, with no weekly archives/notices.
    completed=0
    cutoff=(date.fromisoformat(today())-timedelta(days=14)).isoformat()
    requested={r['user_id']:r['requested_at'] for r in rows('SELECT * FROM arxiv_trend_requests')}
    users=rows('SELECT u.id FROM users u WHERE disabled=0 AND (EXISTS(SELECT 1 FROM arxiv_trend_requests r WHERE r.user_id=u.id) OR EXISTS(SELECT 1 FROM interactions i WHERE i.user_id=u.id AND i.created_at>=?) OR EXISTS(SELECT 1 FROM chat_sessions c WHERE c.user_id=u.id AND c.created_at>=?) OR EXISTS(SELECT 1 FROM interest_profile p WHERE p.user_id=u.id AND p.created_at>=?))',(cutoff,cutoff,cutoff))
    for user in users:
        if await ensure_direction_trend(user['id']):completed+=1
        if user['id'] in requested:
            execute('DELETE FROM arxiv_trend_requests WHERE user_id=? AND requested_at=?',(user['id'],requested[user['id']]))
    return completed
