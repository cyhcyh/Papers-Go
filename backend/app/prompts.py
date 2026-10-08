"""Central default instructions, with administrator overrides in the shared database."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import json
import tomllib

from .db import one

PROMPT_DEFAULTS = tomllib.loads(Path(__file__).with_suffix('.toml').read_text(encoding='utf-8'))
from .agent_skills import builtin, system_instruction
SKILL_DEFAULTS = {'quality': builtin()}
# One snapshot preserves text and revision consistency across all task instructions.
# Existing overrides, including quality, keep their original keys and storage.
DEFAULTS = {**PROMPT_DEFAULTS, **SKILL_DEFAULTS}
_snapshot = ContextVar('prompt_snapshot', default=None)
_revisions = ContextVar('prompt_revisions', default=None)
_skill_state = ContextVar('skill_enabled_snapshot', default=None)


def configuration():
    saved = one("SELECT value FROM app_settings WHERE name='prompts'")
    overrides = json.loads(saved['value']) if saved else {}
    values = {key: overrides.get(key, entry['text']) for key, entry in DEFAULTS.items()}
    skill = system_instruction('quality')
    if skill: values['quality'] = skill['text']
    return values


def get(key):
    frozen = _snapshot.get()
    return (frozen if frozen is not None else configuration())[key]


def revisions():
    saved=one("SELECT value FROM app_settings WHERE name='prompt_versions'")
    versions=json.loads(saved['value']) if saved else {}
    result = {key:versions.get(key,entry.get('version','default-v1')) for key,entry in DEFAULTS.items()}
    skill = system_instruction('quality')
    if skill: result['quality'] = skill['revision']
    return result


def revision(key):
    return (_revisions.get() or revisions())[key]


def skill_enabled(key):
    frozen=_skill_state.get()
    if frozen is not None and key in frozen:return frozen[key]
    instruction=system_instruction(key)
    return instruction['enabled'] if instruction else True


@contextmanager
def snapshot():
    if _snapshot.get() is not None:
        yield
        return
    token = _snapshot.set(configuration())
    version_values = revisions()
    skill = system_instruction('quality')
    if skill:
        # Pin body and revision from the same immutable package, even across an admin save.
        _snapshot.get()['quality'] = skill['text']
        version_values['quality'] = skill['revision']
    state_token = _skill_state.set({'quality':skill['enabled'] if skill else True})
    version_token = _revisions.set(version_values)
    try:
        yield
    finally:
        _snapshot.reset(token)
        _revisions.reset(version_token)
        _skill_state.reset(state_token)


def public_configuration(kind='prompt'):
    values = configuration()
    entries = SKILL_DEFAULTS if kind == 'skill' else DEFAULTS if kind == 'all' else PROMPT_DEFAULTS
    return [{'id': key, 'name': entry['name'], 'description': entry['description'],
             'text': values[key], 'default': entry['text'], 'customized': values[key] != entry['text'],
             'bindings': entry.get('bindings', [])}
            for key, entry in entries.items()]
