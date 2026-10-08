from .. import prompts
from ..llm import runtime as models
from datetime import date, timedelta
from ..db import rows, dumps
from ..config import today
from ..llm.provider import cloud
from .profile import current, put_profile, profile_embedding
from .form import ProfileForm, parse_form, render_form, without_background


async def reflect():
    cutoff = (date.fromisoformat(today())-timedelta(days=7)).isoformat()
    for user in rows('SELECT id FROM users WHERE disabled=0'):
        profile = current(user['id'])
        if not profile:
            continue
        events = rows('''SELECT i.action,i.dwell_ms,p.title,p.tldr
            FROM interactions i JOIN papers p ON p.id=i.paper_id
            WHERE i.user_id=? AND i.created_at>=?
            AND i.action IN ('view','skip','like','expand')
            AND (i.action!='like' OR EXISTS(SELECT 1 FROM user_paper_state s
                WHERE s.user_id=i.user_id AND s.paper_id=i.paper_id AND s.liked=1))
            AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo')
            ORDER BY i.id DESC LIMIT 400''',(user['id'],cutoff))
        if not events:
            continue
        response = await models.complete('reflect', [{'role':'system','content':prompts.get('reflect')},
                                        {'role':'user','content':without_background(profile['content'])+'\n行为：'+dumps(events)}],json_mode=True)
        content = str(response['content'])[:12000]
        form = parse_form(content)
        form['description'] = parse_form(profile['content'])['description']
        content = render_form(ProfileForm.model_validate(form), content)
        vector = await profile_embedding(content)
        # Do not overwrite a profile edited while the cloud request was in progress.
        from ..db import connect
        import json
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            newest = db.execute('SELECT id FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(user['id'],)).fetchone()
            if newest['id']==profile['id']:
                put_profile(user['id'],content,json.loads(profile['structured']),'weekly_reflect',vector,db=db)
