from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from ..auth import admin_user, current_user
from .. import prompts
from .. import agent_skills as skills
from .prompts import PromptEdit, save_instruction

router = APIRouter(prefix='/api/admin/skills', dependencies=[Depends(admin_user)])
personal_router = APIRouter(prefix='/api/skills', dependencies=[Depends(current_user)])


class Enabled(BaseModel):
    enabled: bool


class Review(BaseModel):
    approve: bool


class Restore(BaseModel):
    revision: int


@router.post('/validate')
@personal_router.post('/validate')
def validate(body: skills.SkillEdit):
    meta,text,document,resources=skills.validate_edit(body)
    return {**body.model_dump(),'name':meta['name'],'title':meta.get('metadata',{}).get('title',meta['name']),
            'description':meta['description'],'text':text,'document':document,'resources':resources,
            'allowed_tools':meta.get('allowed-tools','').split()}


@router.get('/catalog')
def catalog(scope: str='all',query: str='',page: int=Query(1,ge=1),user=Depends(admin_user)):
    return skills.catalog(user['id'],True,scope,query,page)


@personal_router.get('')
def personal_catalog(query: str='',page: int=Query(1,ge=1),user=Depends(current_user)):
    return skills.catalog(user['id'],False,'personal',query,page)


@router.get('/toolset')
@personal_router.get('/toolset')
def toolset():
    return sorted(skills.CHAT_TOOLS)


@router.post('')
def create_shared(body: skills.SkillEdit,user=Depends(admin_user)):
    return skills.create(body,user['id'],True)


@personal_router.post('')
def create_personal(body: skills.SkillEdit,user=Depends(current_user)):
    return skills.create(body,user['id'])


@router.get('/detail/{ident}')
def admin_detail(ident: str,user=Depends(admin_user)):
    return skills.detail(ident,user['id'],True)


@personal_router.get('/{ident}')
def personal_detail(ident: str,user=Depends(current_user)):
    return skills.detail(ident,user['id'])


@router.put('/detail/{ident}')
def admin_edit(ident: str,body: skills.SkillEdit,user=Depends(admin_user)):
    return skills.save(ident,body,user['id'],True)


@personal_router.put('/{ident}')
def personal_edit(ident: str,body: skills.SkillEdit,user=Depends(current_user)):
    return skills.save(ident,body,user['id'])


@router.post('/detail/{ident}/enabled')
def admin_enabled(ident: str,body: Enabled,user=Depends(admin_user)):
    return skills.set_enabled(ident,body.enabled,user['id'],True)


@personal_router.post('/{ident}/enabled')
def personal_enabled(ident: str,body: Enabled,user=Depends(current_user)):
    return skills.set_enabled(ident,body.enabled,user['id'])


@personal_router.post('/{ident}/propose')
def propose(ident: str,user=Depends(current_user)):
    return skills.propose(ident,user['id'])


@router.post('/detail/{ident}/review')
def review(ident: str,body: Review,user=Depends(admin_user)):
    return skills.review(ident,body.approve,user['id'])


@router.get('/detail/{ident}/versions')
def admin_versions(ident: str,user=Depends(admin_user)):
    return skills.versions(ident,user['id'],True)


@personal_router.get('/{ident}/versions')
def personal_versions(ident: str,user=Depends(current_user)):
    return skills.versions(ident,user['id'])


@router.get('/detail/{ident}/versions/{revision}')
def admin_version(ident: str,revision: int,user=Depends(admin_user)):
    return skills.version_detail(ident,revision,user['id'],True)


@personal_router.get('/{ident}/versions/{revision}')
def personal_version(ident: str,revision: int,user=Depends(current_user)):
    return skills.version_detail(ident,revision,user['id'])


@router.post('/detail/{ident}/restore')
def admin_restore(ident: str,body: Restore,user=Depends(admin_user)):
    return skills.restore(ident,body.revision,user['id'],True)


@personal_router.post('/{ident}/restore')
def personal_restore(ident: str,body: Restore,user=Depends(current_user)):
    return skills.restore(ident,body.revision,user['id'])


@router.delete('/detail/{ident}')
def admin_delete(ident: str,user=Depends(admin_user)):
    return skills.delete(ident,user['id'],True)


@personal_router.delete('/{ident}')
def personal_delete(ident: str,user=Depends(current_user)):
    return skills.delete(ident,user['id'])


@router.get('')
def listing():
    return prompts.public_configuration('skill')


@router.put('/{key}')
def save(key: str, body: PromptEdit):
    if key not in prompts.SKILL_DEFAULTS:
        raise HTTPException(404, 'Skill 不存在')
    return save_instruction(key, body)
