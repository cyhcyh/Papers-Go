"""Delete a configured source and papers with no remaining configured source."""
import json
from pathlib import Path
from fastapi import HTTPException
from .db import connect,dumps
from .config import now,settings
from .source_catalog import invalidate
from .interest.profile import put_profile
from .logs import event


def source_keys(keys):
    keys=sorted(set(keys))
    if not keys:raise HTTPException(400,'请选择分类')
    return keys


def deletion_plan_many(db,keys,paper_ids=None):
    keys=source_keys(keys)
    db.execute('CREATE TEMP TABLE removed_sources(key TEXT PRIMARY KEY)')
    db.executemany('INSERT INTO removed_sources VALUES(?)',[(key,) for key in keys])
    sources=[dict(s) for s in db.execute('SELECT * FROM source_categories WHERE key IN (SELECT key FROM removed_sources) ORDER BY sort_order,key')]
    if len(sources)!=len(keys):raise HTTPException(404,'部分分类已不存在，请刷新列表')
    remaining=[dict(s) for s in db.execute('SELECT * FROM source_categories WHERE key NOT IN (SELECT key FROM removed_sources) ORDER BY sort_order,key')]
    venue_codes={s['code'].casefold():s['code'] for s in remaining if s['kind']=='venue'}
    arxiv_codes={s['code'] for s in remaining if s['kind']=='arxiv'}
    removed_arxiv={s['code'] for s in sources if s['kind']=='arxiv'}
    db.execute('CREATE TEMP TABLE deletion_candidates(id INTEGER PRIMARY KEY)')
    if paper_ids is not None:
        db.executemany('INSERT OR IGNORE INTO deletion_candidates VALUES(?)',[(ident,) for ident in paper_ids])
    else:
        # One pass across arXiv metadata, rather than one full scan per category.
        db.execute("INSERT OR IGNORE INTO deletion_candidates SELECT p.id FROM papers p WHERE p.primary_category IN (SELECT code FROM source_categories WHERE key IN (SELECT key FROM removed_sources) AND kind='arxiv') OR EXISTS(SELECT 1 FROM json_each(COALESCE(p.categories,'[]')) c JOIN source_categories s ON s.code=c.value WHERE s.key IN (SELECT key FROM removed_sources) AND s.kind='arxiv')")
        db.execute("INSERT OR IGNORE INTO deletion_candidates SELECT p.id FROM papers p JOIN source_categories s ON LOWER(CASE WHEN INSTR(p.venue,'.')>0 THEN SUBSTR(p.venue,1,INSTR(p.venue,'.')-1) ELSE p.venue END)=LOWER(s.code) WHERE s.key IN (SELECT key FROM removed_sources) AND s.kind='venue'")
        db.execute("INSERT OR IGNORE INTO deletion_candidates SELECT p.paper_id FROM paper_sources p JOIN source_categories s ON LOWER(p.venue)=LOWER(s.code) WHERE s.key IN (SELECT key FROM removed_sources) AND s.kind='venue'")
    links={}
    for link in db.execute('SELECT s.* FROM paper_sources s JOIN deletion_candidates c ON c.id=s.paper_id'):
        links.setdefault(link['paper_id'],[]).append(dict(link))
    delete=[];keep=[]
    for row in db.execute('SELECT p.id,p.arxiv_id,p.venue,p.venue_year,p.primary_category,p.categories,p.pdf_path FROM papers p JOIN deletion_candidates c ON c.id=p.id'):
        paper=dict(row);codes=set(json.loads(paper['categories'] or '[]'))
        if paper['primary_category']:codes.add(paper['primary_category'])
        codes-=removed_arxiv
        alternatives=[v for v in links.get(paper['id'],[]) if v['venue'] and v['venue'].casefold() in venue_codes]
        old_venue=(paper['venue'] or '').split('.')[0]
        venue=venue_codes.get(old_venue.casefold())
        year=paper['venue_year']
        if not venue and alternatives:
            alternative=alternatives[0];venue=venue_codes[alternative['venue'].casefold()];year=alternative['year']
        if venue or codes&arxiv_codes:
            primary=paper['primary_category'] if paper['primary_category'] in codes&arxiv_codes else next((s['code'] for s in remaining if s['code'] in codes),None)
            keep.append({**paper,'next_venue':venue,'next_year':year if venue else None,'next_primary':primary,'next_categories':sorted(codes)})
        else:delete.append(paper)
    return sources,delete,keep


def deletion_plan(db,key):
    sources,delete,keep=deletion_plan_many(db,[key])
    return sources[0],delete,keep


def deletion_impact(key):
    with connect() as db:
        source,delete,keep=deletion_plan(db,key)
        return {'key':key,'code':source['code'],'label':source['label'],'delete_papers':len(delete),'keep_papers':len(keep)}


def deletion_impact_many(keys):
    with connect() as db:
        sources,delete,keep=deletion_plan_many(db,keys)
        return {'keys':source_keys(keys),'sources':[{'key':s['key'],'code':s['code'],'label':s['label']} for s in sources],
                'delete_papers':len(delete),'keep_papers':len(keep),'confirmation':f'删除 {len(sources)} 个分类'}


def delete_source(key,actor_id,expected=None):
    result=delete_sources([key],actor_id,expected)
    result['deleted']=key
    return result


def apply_papers(db,sources,delete,keep):
    # Do not reuse a deleted paper ID while an old model request might still finish.
    maximum=db.execute('SELECT COALESCE(MAX(id),0) FROM papers').fetchone()[0]
    db.execute("INSERT INTO app_settings VALUES('last_paper_id',?,?) ON CONFLICT(name) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)",(str(maximum),now()))
    db.execute('CREATE TEMP TABLE deleted_papers(id INTEGER PRIMARY KEY)')
    db.executemany('INSERT INTO deleted_papers VALUES(?)',[(p['id'],) for p in delete])
    db.execute('DELETE FROM interactions WHERE target_id IN (SELECT id FROM interactions WHERE paper_id IN (SELECT id FROM deleted_papers))')
    for table,column in [('interactions','paper_id'),('user_paper_state','paper_id'),('notifications','paper_id'),('reading_cards','paper_id'),('paper_topics','paper_id'),('topic_pending_papers','paper_id'),('paper_sources','paper_id'),('paper_categories','paper_id'),('papers_vec','paper_id')]:
        if table=='papers_vec':
            from .vector_store import active
            if active(db):continue
        db.execute('DELETE FROM '+table+' WHERE '+column+' IN (SELECT id FROM deleted_papers)')
    db.execute('DELETE FROM papers WHERE id IN (SELECT id FROM deleted_papers)')
    db.execute('CREATE TEMP TABLE affected_papers(id INTEGER PRIMARY KEY)')
    db.executemany('INSERT INTO affected_papers VALUES(?)',[(p['id'],) for p in delete+keep])
    for source in sources:
        if source['kind']=='venue':
            db.execute('DELETE FROM paper_sources WHERE LOWER(venue)=? AND paper_id IN (SELECT id FROM affected_papers)',(source['code'].casefold(),))
    for paper in keep:
        changed=(paper['venue'] or '').split('.')[0]!=(paper['next_venue'] or '') or paper['primary_category']!=paper['next_primary']
        db.execute('UPDATE papers SET venue=?,venue_year=?,primary_category=?,categories=? WHERE id=?',(paper['next_venue'],paper['next_year'],paper['next_primary'],dumps(paper['next_categories']),paper['id']))
        if changed:
            from .pipeline.fetch import arxiv_identity,conference_links
            if paper['next_venue']:
                link=db.execute('SELECT source_id FROM paper_sources WHERE paper_id=? AND LOWER(venue)=? LIMIT 1',(paper['id'],paper['next_venue'].casefold())).fetchone()
                if link:
                    abs_url,pdf_url=conference_links(link['source_id']);db.execute('UPDATE papers SET abs_url=?,pdf_url=? WHERE id=?',(abs_url,pdf_url,paper['id']))
            else:
                identity=arxiv_identity(paper['arxiv_id'])[0]
                if not identity:
                    link=db.execute('SELECT source_id FROM paper_sources WHERE paper_id=? AND venue IS NULL LIMIT 1',(paper['id'],)).fetchone()
                    identity=arxiv_identity(link['source_id'])[0] if link else None
                if identity:db.execute('UPDATE papers SET abs_url=?,pdf_url=? WHERE id=?',('https://arxiv.org/abs/'+identity,'https://arxiv.org/pdf/'+identity,paper['id']))
            db.execute("UPDATE papers SET classified=0,classification_state='pending' WHERE id=?",(paper['id'],))
            db.execute('DELETE FROM paper_topics WHERE paper_id=?',(paper['id'],));db.execute('DELETE FROM topic_pending_papers WHERE paper_id=?',(paper['id'],))


def update_topics(db,removed_keys):
    unassigned=set()
    for topic in db.execute('SELECT id,category_keys FROM topics').fetchall():
        keys=json.loads(topic['category_keys'] or '[]')
        if removed_keys.intersection(keys):
            keys=[k for k in keys if k not in removed_keys];db.execute('UPDATE topics SET category_keys=? WHERE id=?',(dumps(keys),topic['id']))
            if not keys:
                unassigned.add(topic['id'])
    return unassigned


def clean_profile(db,profile,removed_keys,unassigned):
    data=json.loads(profile['structured']);before=dumps(data);selection=data.get('category_selection')
    if selection:
        selected=bool(removed_keys.intersection(selection.get('categories',[])) or removed_keys.intersection(selection.get('topics',{})))
        selection['categories']=[k for k in selection.get('categories',[]) if k not in removed_keys]
        for key in removed_keys:
            selection.get('topics',{}).pop(key,None);selection.get('weights',{}).pop(key,None)
        if selected and not selection['categories'] and not any(selection['topics'].values()):data['source_selection_removed']=True
    if before!=dumps(data):put_profile(profile['user_id'],profile['content'],data,'source_removed',profile['embedding'],db)


def finish_sources(db,sources,refresh_stats=True):
    for source in sources:
        if source['kind']=='venue':
            db.execute('DELETE FROM conference_sync WHERE LOWER(venue)=?',(source['code'].casefold(),))
        else:
            db.execute('DELETE FROM arxiv_cursors WHERE category=?',(source['code'],))
            db.execute('DELETE FROM arxiv_window_checks WHERE category=?',(source['code'],))
    db.execute('DELETE FROM source_categories WHERE key IN (SELECT value FROM json_each(?))',(dumps([s['key'] for s in sources]),))
    db.execute("UPDATE direction_trends SET items_json='[]',summary='',evidence='[]',attempted_at=NULL,material_id=NULL")
    if refresh_stats:
        db.execute('DELETE FROM topic_daily_stats')
        db.execute('INSERT INTO topic_daily_stats SELECT COALESCE(p.published,p.ingested_date),pt.topic_id,COUNT(*),AVG(CASE WHEN p.scored=1 THEN p.quality_score END) FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id GROUP BY COALESCE(p.published,p.ingested_date),pt.topic_id')


def remove_pdf(ident,stored,root=None):
    # Metadata must never cause removal outside the application cache.
    root=root or (settings().data_dir/'pdf').resolve()
    # The numeric filename is constructed here, and unlinking an in-cache
    # symlink only removes that link. Stored metadata still needs resolution.
    targets={root/f'{ident}.pdf'}
    if stored and Path(stored) not in targets:targets.add(Path(stored).resolve())
    removed=0
    for target in targets:
        if target.is_relative_to(root) and target.is_file():
            target.unlink();removed+=1
    return removed


def delete_sources(keys,actor_id,expected=None):
    keys=source_keys(keys);removed_keys=set(keys)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        sources,delete,keep=deletion_plan_many(db,keys)
        if expected is not None:
            confirmed=(len(sources)==1 and sources[0]['code']==expected.confirm_code) if hasattr(expected,'confirm_code') else expected.confirm_text==f'删除 {len(sources)} 个分类'
            if not confirmed or len(delete)!=expected.delete_papers or len(keep)!=expected.keep_papers:raise HTTPException(409,'分类或论文数量已变化，请刷新删除预览后重新确认')
        apply_papers(db,sources,delete,keep)
        unassigned=update_topics(db,removed_keys)
        for profile in db.execute('SELECT * FROM interest_profile WHERE id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id)').fetchall():
            clean_profile(db,profile,removed_keys,unassigned)
        finish_sources(db,sources)
    invalidate()
    removed=0
    for paper in delete:
        try:removed+=remove_pdf(paper['id'],paper['pdf_path'])
        except OSError:pass
    event('admin','删除分类及独占论文',user_id=actor_id,source_keys=keys,deleted_papers=len(delete),retained_papers=len(keep),removed_pdf_files=removed)
    return {'deleted':keys,'delete_papers':len(delete),'keep_papers':len(keep),'removed_pdf_files':removed,'_deleted_ids':[p['id'] for p in delete]}
