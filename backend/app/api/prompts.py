from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from ..auth import admin_user
from .. import prompts
from ..config import now
from ..db import connect, dumps

router = APIRouter(prefix='/api/admin/prompts', dependencies=[Depends(admin_user)])


class PromptEdit(BaseModel):
    text: str = Field(min_length=1, max_length=16000)


@router.get('')
def listing():
    return prompts.public_configuration()


@router.put('/{key}')
def save(key: str, body: PromptEdit):
    # Keep old quality URLs usable for existing clients; new UI uses /skills.
    return save_instruction(key, body)


def save_instruction(key: str, body: PromptEdit):
    if key not in prompts.DEFAULTS:
        raise HTTPException(404, 'Prompt 不存在')
    if not body.text.strip():
        raise HTTPException(400, 'Prompt 不能为空')
    if key == 'quality':
        from ..agent_skills import detail, save, SkillEdit
        item = detail('quality', admin=True)
        save('quality', SkillEdit(name=item['name'], title=item['title'], description=item['description'],
             text=body.text, resources=item['resources'], expected_revision=item['revision']), None, True)
        return next(item for item in prompts.public_configuration('all') if item['id'] == key)
    import json
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute("SELECT value FROM app_settings WHERE name='prompts'").fetchone()
        values = json.loads(old['value']) if old else {}
        changed = body.text != values.get(key, prompts.DEFAULTS[key]['text'])
        if body.text == prompts.DEFAULTS[key]['text']:
            values.pop(key, None)
        else:
            values[key] = body.text
        db.execute("INSERT INTO app_settings VALUES('prompts',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (dumps(values), now()))
        old_versions=db.execute("SELECT value FROM app_settings WHERE name='prompt_versions'").fetchone()
        versions=json.loads(old_versions['value']) if old_versions else {}
        if key not in ('trend_report','trend_rewrite') or changed:
            # A second edit during generation must invalidate the first snapshot,
            # even when both saves happen within the same wall-clock second.
            versions[key]=datetime.now(timezone.utc).isoformat() if key in ('trend_report','trend_rewrite') else now()
        db.execute("INSERT INTO app_settings VALUES('prompt_versions',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(dumps(versions),now()))
    return next(item for item in prompts.public_configuration('all') if item['id'] == key)
