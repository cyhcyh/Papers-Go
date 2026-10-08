"""Official announcement batches. Never derive announcement dates from submission dates."""
import json
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

import httpx
from .. import arxiv_client
from ..config import now
from ..db import connect, one, rows, dumps
from ..source_catalog import sources_to_fetch
from ..logs import event
from ..pipeline_control import check_cancelled

SCHEMA = '''
CREATE TABLE IF NOT EXISTS arxiv_batches(
 id INTEGER PRIMARY KEY, announcement_date TEXT NOT NULL, scope TEXT NOT NULL,
 expected_count INTEGER NOT NULL, complete INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, checked_at TEXT NOT NULL,
 UNIQUE(announcement_date,scope));
CREATE TABLE IF NOT EXISTS arxiv_batch_papers(
 batch_id INTEGER NOT NULL REFERENCES arxiv_batches(id) ON DELETE CASCADE,
 arxiv_id TEXT NOT NULL, paper_id INTEGER REFERENCES papers(id) ON DELETE CASCADE,
 PRIMARY KEY(batch_id,arxiv_id));
CREATE INDEX IF NOT EXISTS idx_arxiv_batch_paper ON arxiv_batch_papers(paper_id,batch_id);
CREATE TABLE IF NOT EXISTS arxiv_trend_requests(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, requested_at TEXT NOT NULL);
'''


def initialize(db):
    db.executescript(SCHEMA)
    # Only changes to this day's evidence invalidate its analysis, not likes,
    # vector builds, historical imports or other days' classifications.
    for table, column, ops in (
        ('papers', 'id', ('UPDATE OF title,abstract,classified,classification_state,brief_json',)),
        ('paper_topics', 'paper_id', ('INSERT', 'DELETE', 'UPDATE'))):
        for op in ops:
            name=table+'_'+op.split()[0].lower()
            ref='OLD' if op=='DELETE' else 'NEW'
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS daily_revision_{name} AFTER {op} ON {table}
                BEGIN UPDATE arxiv_batches SET revision=revision+1 WHERE id IN
                (SELECT batch_id FROM arxiv_batch_papers WHERE paper_id={ref}.{column}); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS daily_revision_membership_delete AFTER DELETE ON arxiv_batch_papers
        BEGIN UPDATE arxiv_batches SET revision=revision+1 WHERE id=OLD.batch_id; END''')
    # Topic edits affect descriptions and scope even when the paper link stays.
    db.execute('''CREATE TRIGGER IF NOT EXISTS daily_revision_topic AFTER UPDATE OF name_zh,name_en,description,status ON topics
        BEGIN UPDATE arxiv_batches SET revision=revision+1 WHERE id IN
        (SELECT b.batch_id FROM arxiv_batch_papers b JOIN paper_topics pt ON pt.paper_id=b.paper_id WHERE pt.topic_id=NEW.id); END''')


def scope():
    return dumps(sorted(s['code'] for s in sources_to_fetch('arxiv')))


def latest():
    return one('SELECT * FROM arxiv_batches WHERE scope=? AND complete=1 AND expected_count>0 ORDER BY announcement_date DESC LIMIT 1',(scope(),))


def parse_rss(text):
    root=ET.fromstring(text)
    channel=root.find('channel') if root.tag=='rss' else None
    if channel is None:raise ValueError('arXiv 公告没有返回 RSS channel')
    stamp=channel.findtext('pubDate')
    if not stamp:raise ValueError('arXiv 公告缺少官方公告日期')
    def date_of(value):
        parsed=parsedate_to_datetime(value)
        if parsed.tzinfo is None:raise ValueError('公告日期缺少时区')
        return parsed.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    day=date_of(stamp); identifiers=set()
    for item in channel.findall('item'):
        kind=item.findtext('{http://arxiv.org/schemas/atom}announce_type')
        if kind not in ('new','cross','replace','replace-cross'):
            raise ValueError('arXiv 公告条目类型无法确认')
        item_day=item.findtext('pubDate')
        if not item_day or date_of(item_day)!=day:raise ValueError('arXiv 公告日期不一致')
        match=re.fullmatch(r'oai:arXiv.org:(\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})v(\d+)',item.findtext('guid','').strip())
        if not match:raise ValueError('arXiv 公告条目缺少有效 ID')
        # A later cross-listing is not a new submission. Only the official
        # "new" announcement counts; duplicate listings are collapsed by ID.
        if kind=='new' and int(match[2])==1:identifiers.add(match[1])
    return day,identifiers


async def sync_latest(*, force=False):
    from .fetch import parse_atom, upsert_paper, HEADERS
    selected=scope(); codes=json.loads(selected)
    if not codes:return None
    from datetime import datetime, timedelta
    check=one("SELECT value,updated_at FROM app_settings WHERE name='arxiv_daily_check'")
    if not force and check and check['value']==selected and datetime.fromisoformat(now())-datetime.fromisoformat(check['updated_at'])<timedelta(minutes=30):return latest()
    days=set(); ids=set()
    async with httpx.AsyncClient(timeout=60,headers=HEADERS) as client:
        for code in codes:
            check_cancelled()
            response=await arxiv_client.get(client,'https://rss.arxiv.org/rss/'+quote(code),retries=1)
            day,found=parse_rss(response.text);days.add(day);ids.update(found)
        if len(days)!=1:raise ValueError('各分类的 arXiv 公告尚未同步到同一天，保留上一期')
        day=days.pop()
        # Zero announcements on a weekend must not erase the latest evidence.
        if ids:
            known={r['source_id']:r['paper_id'] for r in rows('SELECT source_id,paper_id FROM paper_sources WHERE source_id IN ('+','.join('?' for _ in ids)+')',list(ids))}
            # Older ingests may only have papers.arxiv_id, without paper_sources.
            for p in rows('SELECT id,arxiv_id FROM papers WHERE arxiv_id IN ('+','.join('?' for _ in ids)+')',list(ids)):known.setdefault(p['arxiv_id'],p['id'])
            retired={r['source_id'] for r in rows('SELECT source_id FROM retired_paper_sources WHERE source_id IN ('+','.join('?' for _ in ids)+')',list(ids))}
            missing=sorted(ids-known.keys()-retired)
            for offset in range(0,len(missing),100):
                check_cancelled();chunk=missing[offset:offset+100]
                response=await arxiv_client.get(client,'https://export.arxiv.org/api/query',retries=1,params={'id_list':','.join(i+'v1' for i in chunk),'max_results':len(chunk)})
                papers=parse_atom(response.text)
                if {p['arxiv_id'] for p in papers}!=set(chunk):raise ValueError('最新公告的论文元数据尚未补齐')
                with connect() as db:
                    for paper in papers:upsert_paper(paper,db)
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if scope()!=selected:raise ValueError('抓取分类已变更，稍后重新同步公告')
                known.update({r['source_id']:r['paper_id'] for r in db.execute('SELECT source_id,paper_id FROM paper_sources WHERE source_id IN ('+','.join('?' for _ in ids)+')',list(ids))})
                effective=ids-retired
                if effective-known.keys():raise ValueError('最新公告仍有缺失论文')
                db.execute('INSERT INTO arxiv_batches(announcement_date,scope,expected_count,checked_at) VALUES(?,?,?,?) ON CONFLICT(announcement_date,scope) DO UPDATE SET checked_at=excluded.checked_at',(day,selected,len(effective),now()))
                batch_id=db.execute('SELECT id FROM arxiv_batches WHERE announcement_date=? AND scope=?',(day,selected)).fetchone()['id']
                old={r['arxiv_id'] for r in db.execute('SELECT arxiv_id FROM arxiv_batch_papers WHERE batch_id=?',(batch_id,))}
                if old!=effective:
                    db.execute('DELETE FROM arxiv_batch_papers WHERE batch_id=?',(batch_id,))
                    db.executemany('INSERT INTO arxiv_batch_papers VALUES(?,?,?)',[(batch_id,i,known[i]) for i in sorted(effective)])
                    db.execute('UPDATE arxiv_batches SET revision=revision+1 WHERE id=?',(batch_id,))
                db.execute('UPDATE arxiv_batches SET complete=1,expected_count=? WHERE id=?',(len(effective),batch_id))
            event('trend','最新 arXiv 公告已同步',job='trend_report',announcement_date=day,papers=len(effective),categories=codes)
    with connect() as db:
        db.execute("INSERT INTO app_settings(name,value,updated_at) VALUES('arxiv_daily_check',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(selected,now()))
        # Keep a bounded announcement ledger; paper records are never deleted.
        keeper=latest()
        db.execute("DELETE FROM arxiv_batches WHERE announcement_date<date(?,'-30 days') AND id!=?",(day,keeper['id'] if keeper else 0))
        db.execute("DELETE FROM direction_trends WHERE audience LIKE 'arxiv-daily:%' AND period_end<date(?,'-30 days') AND period_end!=?",(day,keeper['announcement_date'] if keeper else ''))
    return latest()
