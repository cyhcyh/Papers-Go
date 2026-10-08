from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import ValidationError

from .. import task_settings
from ..logs import event
from ..db import rows

router = APIRouter(prefix='/task-center')


@router.get('')
def task_configuration():
    return task_settings.public_configuration()


@router.get('/metric-users')
def metric_users(query: str = Query('', max_length=100)):
    # Load a small searchable list only when the administrator opens the filter.
    prefix = query.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return rows("SELECT id,username FROM users WHERE username LIKE ? ESCAPE '\\' ORDER BY username LIMIT 30", (prefix+'%',))


@router.patch('/schedules')
def schedules(body: dict[task_settings.ScheduleName, task_settings.Schedule]):
    result = task_settings.update_schedules(body)
    event('task', '管理员修改任务计划', jobs=list(body))
    return result


@router.patch('/advanced/{name}')
def advanced(name: task_settings.AdvancedName, body: dict = Body(...)):
    values = dict(body)
    clear = values.pop('clear_secret', False)
    if not isinstance(clear, bool):
        raise HTTPException(422, '清除凭据选项必须为布尔值')
    try:
        validated = task_settings.ADVANCED_MODELS[name].model_validate(values)
        result = task_settings.update_advanced(name, validated.model_dump(exclude_unset=True), clear_secret=clear)
    except ValidationError as error:
        # Never return a validation object's input, which may contain a credential.
        raise HTTPException(422, '参数格式或范围不正确，请检查后重试') from error
    event('task', '管理员修改任务高级设置', job=name, fields=list(values), credential_cleared=clear)
    return result
