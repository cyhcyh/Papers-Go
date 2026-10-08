"""Configuration-only preflight shared by manual, scheduled and startup jobs."""
from fastapi import HTTPException
from .llm import runtime as models
from .pipeline_control import enabled_jobs
from .task_settings import PIPELINE_JOBS
from .db import execute, dumps
from .logs import event

DEPENDENCIES={
    'classify':('classify','embedding'),
    'build_vectors':('embedding',),
    'assess_quality':('quality',),
    'tldr_gen':('brief',),
    'preread':('reading_l2',),
    'trend_report':('trend_report','classify','embedding'),
    'reflect':('reflect','embedding'),
    'audit':('audit',),
}


def missing_configuration(names,config=None,switches=None):
    config=config if config is not None else models.configuration()
    switches=switches if switches is not None else enabled_jobs()
    missing={}
    checked=set()
    for name in names:
        if not switches.get(name,True):continue
        for feature in DEPENDENCIES.get(name,()):
            if (name=='trend_report' and feature in ('classify','embedding') and not switches.get('classify',True)):continue
            if feature not in checked:
                checked.add(feature)
                reason=models.configuration_issue(feature,config)
                if reason:missing[feature]={'feature':feature,'name':models.FEATURES[feature][0],'reason':reason}
    return list(missing.values())


def require_configuration(name):
    missing=missing_configuration(PIPELINE_JOBS if name=='pipeline' else [name])
    if missing:raise HTTPException(409,detail={'code':'model_configuration_missing','message':'请先配置以下任务的模型','missing':missing})


def ready_for_automatic(names,*,label):
    missing=missing_configuration(names)
    if not missing:return True
    message='待配置：'+'、'.join(item['name']+'（'+item['reason']+'）' for item in missing)
    # Only publish changes. An idle worker can revisit a pending trend request
    # without repeatedly writing the same warning or filling the log.
    from .db import one
    saved=one('SELECT error FROM source_status WHERE name=?',(label,))
    if not saved or saved['error']!=message:
        execute('INSERT INTO source_status(name,error,running) VALUES(?,?,0) ON CONFLICT(name) DO UPDATE SET error=excluded.error',(label,message))
        event('task','配置未齐，本次自动执行已跳过',job=label,missing=dumps(missing))
    return False
