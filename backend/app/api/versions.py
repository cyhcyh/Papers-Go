from fastapi import APIRouter, Depends

from ..auth import admin_user
from .. import versioning

router = APIRouter()


@router.get('/api/version')
def installed_version():
    return versioning.local_metadata()


@router.get('/api/admin/version', dependencies=[Depends(admin_user)])
async def check_version(force: bool = False):
    return await versioning.check_version(force)


@router.get('/api/admin/version/changelog', dependencies=[Depends(admin_user)])
async def changelog(force: bool = False):
    return await versioning.remote_changelog(force)
