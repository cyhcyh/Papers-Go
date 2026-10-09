"""Small, shared task configuration; credentials never leave the server."""
import copy
import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import now, settings
from .db import connect, dumps, one
from .llm.secrets import cipher, SecretStorageError

PIPELINE_JOBS = ('fetch_arxiv', 'fetch_conf', 'fetch_community', 'classify', 'build_vectors',
                 'assess_quality', 'tldr_gen', 'preread', 'trend_stats', 'alert_eval', 'metrics')
INDEPENDENT_JOBS = ('author_impact', 'paper_expiry', 'trend_report', 'reflect', 'audit')
ScheduleName = Literal['pipeline', 'author_impact', 'paper_expiry', 'trend_report', 'reflect', 'audit']
AdvancedName = Literal['fetch_community', 'audit', 'author_impact', 'paper_expiry']
SECRET_FIELDS = {'fetch_community': 'github_token', 'author_impact': 'openalex_api_key'}


class Schedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    frequency: Literal['daily', 'weekly'] = 'daily'
    time: str = Field('13:30', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    weekdays: list[int] = Field(default_factory=lambda: [6], max_length=7)

    @field_validator('weekdays')
    @classmethod
    def valid_days(cls, value):
        if any(day < 0 or day > 6 for day in value):
            raise ValueError('星期必须在周一至周日范围内')
        return sorted(set(value))

    @model_validator(mode='after')
    def weekly_days(self):
        if self.frequency == 'weekly' and not self.weekdays:
            raise ValueError('每周执行至少选择一个星期')
        return self


class Community(BaseModel):
    model_config = ConfigDict(extra='forbid')
    github_token: str = Field('', max_length=1000)
    lookback_days: int = Field(14, ge=1, le=90)
    cache_hours: int = Field(24, ge=1, le=168)


class Audit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample_size: int = Field(50, ge=1, le=100)


class AuthorImpact(BaseModel):
    model_config = ConfigDict(extra='forbid')
    openalex_api_key: str = Field('', max_length=1000)
    batch_size: int = Field(100, ge=1, le=1000)
    cache_days: int = Field(180, ge=30, le=365)
    priority_category: str = Field('', max_length=40, pattern=r'^(?:[A-Za-z][A-Za-z0-9.-]*)?$')


class Expiry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    batch_size: int = Field(50, ge=1, le=200)
    initial_days: int = Field(365, ge=1, le=36500)
    social_days: int = Field(730, ge=1, le=36500)
    # The existing per-user counter has a database constraint of 0..5.
    repeat_limit: int = Field(5, ge=0, le=5)


ADVANCED_MODELS = {'fetch_community': Community, 'audit': Audit,
                   'author_impact': AuthorImpact, 'paper_expiry': Expiry}


def defaults():
    cfg = settings()
    times = {'pipeline': '10:00', 'author_impact': '06:20', 'paper_expiry': '11:00',
             'trend_report': '10:50', 'reflect': '04:00', 'audit': '06:00'}
    schedules = {name: Schedule(time=time, frequency='weekly' if name in ('reflect', 'audit') else 'daily').model_dump()
                 for name, time in times.items()}
    advanced = {name: model().model_dump() for name, model in ADVANCED_MODELS.items()}
    advanced['fetch_community']['github_token'] = cfg.github_token
    advanced['author_impact'].update(openalex_api_key=cfg.openalex_api_key, batch_size=cfg.author_impact_batch_size)
    return {'schedules': schedules, 'advanced': advanced}


def _decode(saved, *, secrets=False):
    result = defaults()
    if saved:
        values = json.loads(saved['value'])
        for group in ('schedules', 'advanced'):
            for name, values_for_name in values.get(group, {}).items():
                if name in result[group]:
                    if group == 'advanced' and name == 'author_impact':
                        values_for_name = {key:value for key,value in values_for_name.items() if key != 'daily_credits'}
                    result[group][name].update(values_for_name)
    for name, field in SECRET_FIELDS.items():
        values = result['advanced'][name]
        token = values.pop(field + '_encrypted', None)
        if token and secrets:
            try:
                values[field] = cipher().decrypt(token.encode()).decode()
            except (InvalidToken, UnicodeError) as error:
                raise SecretStorageError('无法解密任务凭据，请恢复原密钥文件') from error
        elif token:
            values[field + '_configured'] = True
    return result


def configuration():
    return _decode(one("SELECT value FROM app_settings WHERE name='task_center'"), secrets=True)


def schedules():
    saved = one("SELECT value,updated_at FROM app_settings WHERE name='task_center'")
    return _decode(saved)['schedules'], saved['updated_at'] if saved else ''


def trigger(schedule):
    hour, minute = map(int, schedule['time'].split(':'))
    return CronTrigger(hour=hour, minute=minute, timezone=settings().tz,
                       day_of_week=','.join(map(str, schedule['weekdays'])) if schedule['frequency'] == 'weekly' else '*')


def public_configuration():
    saved = one("SELECT value,updated_at FROM app_settings WHERE name='task_center'")
    result = _decode(saved)
    for name, field in SECRET_FIELDS.items():
        values = result['advanced'][name]
        values[field + '_configured'] = bool(values.pop(field, '')) or values.get(field + '_configured', False)
    stamp = datetime.now(ZoneInfo(settings().tz))
    for schedule in result['schedules'].values():
        schedule['next_run'] = trigger(schedule).get_next_fire_time(None, stamp).isoformat() if schedule['enabled'] else None
    result.update(timezone=settings().tz, updated_at=saved['updated_at'] if saved else None)
    # Environment credentials do not become frontend restore-default values.
    result['defaults'] = defaults()
    for name, field in SECRET_FIELDS.items():
        result['defaults']['advanced'][name].pop(field, None)
    return result


def _persist(db, config):
    stored = copy.deepcopy(config)
    for name, field in SECRET_FIELDS.items():
        values = stored['advanced'][name]
        secret = values.pop(field, '')
        values.pop(field + '_configured', None)
        values.pop(field + '_encrypted', None)
        values[field] = ''  # Explicitly override an environment credential when cleared.
        if secret:
            values[field + '_encrypted'] = cipher(create=True).encrypt(secret.encode()).decode()
    stamp = now()
    db.execute("INSERT INTO app_settings(name,value,updated_at) VALUES('task_center',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
               (dumps(stored), stamp))
    for field, key in (('initial_days', 'paper_initial_days'), ('social_days', 'paper_social_days'), ('repeat_limit', 'paper_repeat_limit')):
        db.execute('INSERT INTO app_settings(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                   (key, str(config['advanced']['paper_expiry'][field]), stamp))


def update_schedules(values):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        config = _decode(db.execute("SELECT value FROM app_settings WHERE name='task_center'").fetchone(), secrets=True)
        for name, value in values.items():
            config['schedules'][name] = value.model_dump()
        _persist(db, config)
    return public_configuration()


def update_advanced(name, values, *, clear_secret=False):
    # Validate only the changed group, then merge with the latest stored configuration.
    if name == 'author_impact':
        values.pop('daily_credits', None)  # Discard the removed field from an already open browser form.
    field = SECRET_FIELDS.get(name)
    if field and values.get(field):
        values[field] = values[field].strip()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        config = _decode(db.execute("SELECT value FROM app_settings WHERE name='task_center'").fetchone(), secrets=True)
        current = config['advanced'][name]
        if field and not values.get(field):
            values.pop(field, None)  # An empty password input preserves the existing credential.
        current.update(values)
        if field and clear_secret:
            current[field] = ''
        config['advanced'][name] = ADVANCED_MODELS[name].model_validate(current).model_dump()
        _persist(db, config)
    return public_configuration()
