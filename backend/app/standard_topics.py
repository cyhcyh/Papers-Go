"""Research areas stored in SQLite; bundled reference data is immutable."""
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from .config import now, settings
from .db import connect, dumps
from .taxonomy import paper_keys
from .source_catalog import registry
from .disciplines import DISCIPLINES, ID_PREFIXES

FIELDS = ('id', 'discipline', 'name', 'description')
ZH = {'Ramsey Theory':'Ramsey 理论','Reinforcement Learning':'强化学习',
      'Machine Learning':'机器学习','Computer Vision':'计算机视觉',
      'Natural Language Processing':'自然语言处理','Continual Learning':'持续学习',
      'Representation Learning':'表示学习','Multi-Agent Systems':'多智能体系统',
      'Extremal Graph Theory':'极值图论','Enumerative Combinatorics':'枚举组合'}

def words(text):
    stop=set('the and for with from using based into study analysis approach method model system general other none above related applications including theory problems'.split())
    return {w[:-1] if w.endswith('s') and len(w)>4 else w for w in re.findall(r'[a-z]{3,}',text.lower()) if w not in stop}

def normalized_name(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',value).casefold() if c.isalnum())

def validate_area(area):
    if set(area)!=set(FIELDS) or area['discipline'] not in DISCIPLINES:
        raise ValueError('研究方向必须使用 id、discipline、name、description 四字段')
    if not all(isinstance(area[k],str) and area[k].strip() for k in FIELDS):raise ValueError('研究方向字段不能为空')
    # The expanded seed uses QB/QF; existing QBIO/QFIN identities stay valid.
    if not re.fullmatch(r'RA-(?:'+ '|'.join((*ID_PREFIXES.values(),'QB','QF','LOCAL')) +r')-\d{3,}',area['id']):raise ValueError('研究方向 ID 无效')
    if len(area['name'])>200 or len(area['description'])>1200:raise ValueError('研究方向名称或描述过长')
    return {k:area[k].strip() for k in FIELDS}

def entry(area):
    return {**area,'key':area['id'],'code':area['id'],'system':area['discipline'],
            'label':area['name'],'name_zh':ZH.get(area['name'],area['name']),
            'parent':None,'has_children':False,
            'path':area['discipline']+' › '+area['name']+' — '+area['description']}

def revision(db):
    row=db.execute("SELECT value FROM app_settings WHERE name='research_area_revision'").fetchone()
    return int(row['value']) if row else 0

def bump(db):
    db.execute("INSERT INTO app_settings(name,value,updated_at) VALUES('research_area_revision','1',?) ON CONFLICT(name) DO UPDATE SET value=CAST(value AS INTEGER)+1,updated_at=excluded.updated_at",(now(),))

@lru_cache(maxsize=4)
def _catalog(path,version):
    with connect() as db:return {a['id']:entry(dict(a)) for a in db.execute('SELECT * FROM research_areas ORDER BY id')}

def catalog():
    with connect() as db:version=revision(db)
    return _catalog(str(settings().data_dir.resolve()),version)

def search(query='',system='',parent=None,limit=200):
    needle=query.strip().casefold()
    values=[e for e in catalog().values() if (not system or e['discipline']==system)
            and (not needle or needle in (e['key']+' '+e['label']+' '+e['name_zh']+' '+e['description']).casefold())]
    return sorted(values,key=lambda e:(e['label'].casefold()!=needle,e['code']))[:limit]

def blocked_topics(db):
    return [dict(t) for t in db.execute("SELECT * FROM topics WHERE status IN ('disabled','merged','legacy')")]

def equivalent_name(a,b):
    from .topic_retrieval import tokens
    return normalized_name(a)==normalized_name(b) or bool(tokens(a)) and set(tokens(a))==set(tokens(b))

def blocked_entry(area,blocked):
    return any(t.get('standard_key')==area['key'] and area['key'] is not None or equivalent_name(area['label'],t['name_en'])
               or normalized_name(area.get('name_zh',''))==normalized_name(t['name_zh']) for t in blocked)

def candidates(paper,blocked=(),limit=40,semantic_scores=None):
    keys=paper_keys(paper)
    if not keys:return []
    from .topic_retrieval import retrieve
    with connect() as db:disabled=blocked_topics(db)
    pool=[e for e in catalog().values() if e['key'] not in blocked and not blocked_entry(e,disabled)]
    sources=[s for s in registry() if s['key'] in keys]
    disciplines={s['discipline'] for s in sources}
    labels=[s['label_en'] for s in sources if s['kind']=='arxiv']
    return retrieve(paper,pool,limit,semantic_scores,disciplines,labels)

def scope_keys(area,keys):
    return sorted(set(keys)&{s['key'] for s in registry()})

def allocate_id(db,discipline):
    if discipline not in DISCIPLINES:raise ValueError('研究方向学科无效')
    from .research_catalog import allocate_local_id
    return allocate_local_id(db)


def save_area(db,key,discipline,name,description):
    area=validate_area({'id':key or allocate_id(db,discipline),'discipline':discipline,'name':name,'description':description})
    for existing in db.execute('SELECT id,name FROM research_areas WHERE id!=?',(area['id'],)):
        if equivalent_name(area['name'],existing['name']):raise ValueError('已有同名研究方向，请编辑或恢复已有主题')
    db.execute('INSERT INTO research_areas VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET discipline=excluded.discipline,name=excluded.name,description=excluded.description',tuple(area.values()))
    from .research_catalog import record_edit
    record_edit(db,area)
    bump(db);return entry(area)

def queue_or_assign(db,paper,area,confidence,name_zh=None):
    if blocked_entry(area,blocked_topics(db)):raise ValueError('该主题已停用')
    keys=scope_keys(area,paper_keys(paper))
    if not keys:raise ValueError('论文不属于已开放分类')
    topic=db.execute('SELECT * FROM topics WHERE standard_key=?',(area['key'],)).fetchone()
    if not topic:
        ident=db.execute("INSERT INTO topics(name_zh,name_en,status,created_by,created_at,category_keys,standard_key,standard_system,standard_code,standard_path,discipline,description) VALUES(?,?,'proposed','agent',?,?,?,?,?,?,?,?)",
                         ((name_zh or area['name_zh']).strip()[:100],area['label'],now(),dumps(keys),area['key'],area['discipline'],area['key'],area['path'],area['discipline'],area['description'])).lastrowid
        topic=db.execute('SELECT * FROM topics WHERE id=?',(ident,)).fetchone()
    return attach(db,paper,topic,confidence,keys)

def attach(db,paper,topic,confidence,keys):
    ident=topic['id']
    if topic['status'] in ('disabled','merged','legacy'):raise ValueError('该主题已停用')
    existing=set(json.loads(topic['category_keys'] or '[]'))
    db.execute('UPDATE topics SET category_keys=? WHERE id=?',(dumps(sorted(existing|set(keys))),ident))
    db.execute('DELETE FROM paper_topics WHERE paper_id=?',(paper['id'],));db.execute('DELETE FROM topic_pending_papers WHERE paper_id=?',(paper['id'],))
    if topic['status']=='active':
        db.execute('INSERT INTO paper_topics VALUES(?,?,?)',(paper['id'],ident,confidence));state='ready'
    else:
        db.execute('INSERT INTO topic_pending_papers VALUES(?,?,?,?)',(paper['id'],ident,confidence,now()));state='awaiting_approval'
    db.execute('UPDATE papers SET classified=1,classification_state=? WHERE id=?',(state,paper['id']))
    return ident,state

def queue_new_topic(db,paper,draft,confidence):
    area={'key':None,'label':draft['name'],'name_zh':draft['name_zh']}
    if blocked_entry(area,blocked_topics(db)):raise ValueError('拟定的主题与已停用主题重复')
    existing=next((dict(t) for t in db.execute('SELECT * FROM topics') if equivalent_name(t['name_en'],draft['name']) or normalized_name(t['name_zh'])==normalized_name(draft['name_zh'])),None)
    if not existing:
        found=next((entry(dict(a)) for a in db.execute('SELECT * FROM research_areas') if equivalent_name(a['name'],draft['name'])),None)
        if found:return queue_or_assign(db,paper,found,confidence,draft['name_zh'])
        ident=db.execute("INSERT INTO topics(name_zh,name_en,status,created_by,created_at,category_keys,discipline,description,proposal_reason) VALUES(?,?,'proposed','agent',?,?,?,?,?)",
                         (draft['name_zh'],draft['name'],now(),dumps(sorted(paper_keys(paper))),draft['discipline'],draft['description'],draft['novelty_reason'])).lastrowid
        existing=db.execute('SELECT * FROM topics WHERE id=?',(ident,)).fetchone()
    return attach(db,paper,existing,confidence,paper_keys(paper))

def approve(db,topic_id):
    topic=db.execute('SELECT * FROM topics WHERE id=?',(topic_id,)).fetchone()
    if not topic:raise ValueError('主题不存在')
    area=save_area(db,topic['standard_key'],topic['discipline'],topic['name_en'],topic['description'])
    db.execute("UPDATE topics SET status='active',standard_key=?,standard_code=?,standard_system=?,standard_path=? WHERE id=?",(area['key'],area['key'],area['discipline'],area['path'],topic_id))
    pending=db.execute('SELECT * FROM topic_pending_papers WHERE topic_id=?',(topic_id,)).fetchall()
    for link in pending:
        db.execute('DELETE FROM paper_topics WHERE paper_id=?',(link['paper_id'],));db.execute('INSERT INTO paper_topics VALUES(?,?,?)',(link['paper_id'],topic_id,link['confidence']))
        db.execute("UPDATE papers SET classified=1,classification_state='ready' WHERE id=?",(link['paper_id'],))
        db.execute("UPDATE paper_classifications SET standard_key=?,status='ready',updated_at=? WHERE paper_id=?",(area['key'],now(),link['paper_id']))
    db.execute('DELETE FROM topic_pending_papers WHERE topic_id=?',(topic_id,));return len(pending)

def clean_profile_references(db,ids):
    from .interest.profile import put_profile
    for p in db.execute('SELECT * FROM interest_profile WHERE id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id)').fetchall():
        data=json.loads(p['structured']);original=dumps(data);data['topic_ids']=[i for i in data.get('topic_ids',[]) if i not in ids]
        selection=data.get('category_selection',{})
        if 'topics' in selection:
            selection['topics']={k:[i for i in value if i not in ids] for k,value in selection['topics'].items()};selection['topics']={k:v for k,v in selection['topics'].items() if v}
        if 'weights' in selection:
            remaining=set(selection.get('categories',[]))|set(selection.get('topics',{}))
            selection['weights']={k:v for k,v in selection['weights'].items() if k in remaining}
        if dumps(data)!=original:put_profile(p['user_id'],p['content'],data,'topic_removed',p['embedding'],db)

def migrate(db):
    db.execute('CREATE TABLE IF NOT EXISTS research_areas(id TEXT PRIMARY KEY,discipline TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL)')
    for name,definition in [('discipline','TEXT'),('description',"TEXT NOT NULL DEFAULT ''"),('proposal_reason',"TEXT NOT NULL DEFAULT ''")]:
        if name not in {r['name'] for r in db.execute('PRAGMA table_info(topics)')}:db.execute(f'ALTER TABLE topics ADD COLUMN {name} {definition}')
    marker='medium_research_areas_v1'
    if db.execute('SELECT 1 FROM app_migrations WHERE name=?',(marker,)).fetchone():return
    source=settings().data_dir/'research_areas.json'
    if not source.is_file():source=Path(__file__).parent/'resources/research_areas.json'
    areas=[validate_area(a) for a in json.loads(source.read_text(encoding='utf-8'))]
    if len({a['id'] for a in areas})!=len(areas) or len({normalized_name(a['name']) for a in areas})!=len(areas):raise ValueError('研究方向目录存在重复')
    db.executemany('INSERT INTO research_areas VALUES(?,?,?,?)',[tuple(a.values()) for a in areas]);bump(db);removed=set()
    for t in db.execute('SELECT * FROM topics').fetchall():
        matching=next((a for a in areas if normalized_name(a['name'])==normalized_name(t['name_en'])),None)
        if matching:
            e=entry(matching);conflict=db.execute('SELECT id FROM topics WHERE standard_key=? AND id!=?',(e['key'],t['id'])).fetchone()
            if not conflict:
                db.execute('UPDATE topics SET parent_id=NULL,standard_key=?,standard_system=?,standard_code=?,standard_path=?,discipline=?,description=? WHERE id=?',(e['key'],e['discipline'],e['key'],e['path'],e['discipline'],e['description'],t['id']))
                if 'topic_key' in {c['name'] for c in db.execute('PRAGMA table_info(watches)')}:
                    db.execute('UPDATE watches SET topic_key=? WHERE topic_key=?',(e['key'],t['standard_key']))
                continue
        removed.add(t['id'])
        db.execute("UPDATE topics SET status='disabled',parent_id=NULL,standard_key=NULL,standard_system=NULL,standard_code=NULL,standard_path=NULL WHERE id=?",(t['id'],))
    db.execute('DELETE FROM paper_topics WHERE topic_id IN (SELECT id FROM topics WHERE standard_key IS NULL)')
    db.execute('DELETE FROM topic_pending_papers');db.execute('DELETE FROM paper_classifications');db.execute('DELETE FROM topic_proposals')
    db.execute("UPDATE papers SET classified=0,classification_state='pending'")
    # Preserve the source scope when an old topic-only subscription is removed.
    for p in db.execute('SELECT * FROM interest_profile WHERE id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id)').fetchall():
        data=json.loads(p['structured']);selection=data.setdefault('category_selection',{'categories':[],'topics':{}})
        partial=selection.get('topics',{});categories=set(selection.get('categories',[]))
        for key,ids in partial.items():
            if set(ids)&removed and not set(ids)-removed:categories.add(key)
        data['category_selection']['categories']=sorted(categories)
        if dumps(data)!=p['structured']:
            from .interest.profile import put_profile
            put_profile(p['user_id'],p['content'],data,'research_area_migration',p['embedding'],db)
    clean_profile_references(db,removed)
    db.execute('UPDATE source_categories SET standard_system=NULL');db.execute('DELETE FROM topic_daily_stats')
    db.execute("UPDATE source_status SET progress=NULL,added=0 WHERE name='classify'");db.execute('UPDATE direction_trends SET attempted_at=NULL')
    saved=db.execute("SELECT value FROM app_settings WHERE name='prompts'").fetchone()
    if saved:
        overrides=json.loads(saved['value']);overrides.pop('classify',None)
        db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='prompts'",(dumps(overrides),now()))
    db.execute('INSERT INTO app_migrations VALUES(?,?)',(marker,now()))
