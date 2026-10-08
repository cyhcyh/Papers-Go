"""Shared ranking weights, read once per request/batch, never once per paper."""
import json
import math
from pydantic import BaseModel, Field, model_validator
from .config import settings, now
from .db import connect, one, dumps


class PersonalWeights(BaseModel):
    interest: float = Field(ge=0, le=1, allow_inf_nan=False)
    quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    diversity: float = Field(ge=0, le=1, allow_inf_nan=False)
    author: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode='after')
    def total(self):
        if not math.isclose(self.interest + self.quality + self.diversity + self.author, 1, abs_tol=1e-8):
            raise ValueError('个人推荐权重合计必须为 100%')
        return self


class GuestWeights(BaseModel):
    quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    recency: float = Field(ge=0, le=1, allow_inf_nan=False)
    author: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode='after')
    def total(self):
        if not math.isclose(self.quality + self.recency + self.author, 1, abs_tol=1e-8):
            raise ValueError('游客推荐权重合计必须为 100%')
        return self


class RecommendationInput(BaseModel):
    personal: PersonalWeights
    guest: GuestWeights
    # Previously loaded browser forms used one independent top-level bonus.
    author: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


def with_author(group, author):
    base = {key: value for key, value in group.items() if key != 'author'}
    total = sum(base.values())
    return {**{key: value * (1-author)/total if total else 0 for key, value in base.items()},
            'author': author}


def defaults():
    cfg = settings()
    return {'personal': with_author({'interest': cfg.interest_weight, 'quality': cfg.quality_weight,
                                    'diversity': cfg.novelty_weight}, cfg.author_weight),
            'guest': with_author({'quality': cfg.guest_quality_weight,
                                 'recency': cfg.guest_recency_weight}, cfg.guest_author_weight)}


def normalized(value):
    fallback = defaults()
    result = {}
    for name in ('personal', 'guest'):
        group = value.get(name, fallback[name])
        result[name] = dict(group) if 'author' in group else with_author(group, value.get('author', fallback[name]['author']))
    return result


def initialize(db):
    saved = db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()
    if not saved:
        return
    site = json.loads(saved['value'])
    if 'recommendation' in site:
        value = normalized(site['recommendation'])
        if value != site['recommendation']:
            site['recommendation'] = value
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='site'", (dumps(site), now()))


def configuration(db=None):
    saved = db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone() if db is not None else one("SELECT value FROM app_settings WHERE name='site'")
    value = json.loads(saved['value']).get('recommendation', defaults()) if saved else defaults()
    return normalized(value)


def cache_key(value):
    return (value['personal']['interest'], value['personal']['quality'],
            value['personal']['diversity'], value['guest']['quality'], value['guest']['recency'],
            value['personal']['author'], value['guest']['author'])


def scoring_settings(value):
    return settings().model_copy(update=dict(zip(
        ('interest_weight', 'quality_weight', 'novelty_weight', 'guest_quality_weight', 'guest_recency_weight', 'author_weight', 'guest_author_weight'),
        cache_key(value))))


def update(body):
    value = body.model_dump(exclude={'author'})
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        site = json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
        previous = normalized(site.get('recommendation', defaults()))
        for name in ('personal', 'guest'):
            if 'author' not in getattr(body, name).model_fields_set:
                coefficient = body.author if body.author is not None else previous[name]['author']
                value[name] = with_author(value[name], coefficient)
        site['recommendation'] = value
        db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='site'", (dumps(site), now()))
    return value
