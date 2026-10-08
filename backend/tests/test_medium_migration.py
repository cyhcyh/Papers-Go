import json
from app.db import connect,execute,one,rows,init_db,pack
from app.config import now,settings
from app.standard_topics import migrate
from app.interest.profile import put_profile,current

def test_old_topic_migration_preserves_matching_ids_and_personal_records(client,accounts,papers):
    uid=accounts[0]['user']['id'];source='arxiv:cs.AI'
    # Model a pre-upgrade database with both a matching concept and a fine topic.
    execute("UPDATE topics SET name_en='Ramsey theory',standard_key='MSC2020:05D10',category_keys='[\"arxiv:math.CO\"]' WHERE id=15")
    execute("UPDATE topics SET name_en='Fine specific old theme',standard_key='CCS2012:old' WHERE id=3")
    execute('INSERT INTO watches(user_id,type,value,topic_id,topic_key,created_at) VALUES(?,\'topic\',\'Ramsey理论\',15,\'MSC2020:05D10\',?)',(uid,now()))
    put_profile(uid,'保留研究描述',{'topic_ids':[3,15],'category_selection':{'categories':[], 'topics':{source:[3],'arxiv:math.CO':[15]},'weights':{source:.3,'arxiv:math.CO':.9}}},'test',pack([1,0,0,0]))
    before=rows('SELECT id,title,authors,abstract FROM papers ORDER BY id')
    execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved) VALUES(?,?,1,1)',(uid,papers[0]))
    execute("DELETE FROM app_migrations WHERE name='medium_research_areas_v1'")
    execute('DELETE FROM research_areas')
    with connect() as db:migrate(db)
    assert one('SELECT standard_key FROM topics WHERE id=15')['standard_key']=='RA-MATH-060'
    assert one('SELECT status,standard_key FROM topics WHERE id=3')=={'status':'disabled','standard_key':None}
    assert rows('SELECT id,title,authors,abstract FROM papers ORDER BY id')==before
    assert one('SELECT liked,saved FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,papers[0]))=={'liked':1,'saved':1}
    profile=current(uid);data=json.loads(profile['structured'])
    assert profile['content']=='保留研究描述' and data['topic_ids']==[15]
    assert source in data['category_selection']['categories'] and data['category_selection']['weights'][source]==.3
    assert one('SELECT topic_key FROM watches WHERE user_id=?',(uid,))['topic_key']=='RA-MATH-060'
    # Restarts do not erase results or repeat the migration.
    execute("UPDATE papers SET classified=1,classification_state='ready' WHERE id=?",(papers[0],))
    init_db(recover=False)
    assert one('SELECT classified FROM papers WHERE id=?',(papers[0],))['classified']==1

def test_startup_never_rewrites_a_legacy_export(client):
    path=settings().data_dir/'research_areas.json'
    path.write_text('[]',encoding='utf-8');init_db(recover=False)
    assert json.loads(path.read_text(encoding='utf-8'))==[]
    assert one('SELECT COUNT(*) n FROM research_areas')['n']==388
