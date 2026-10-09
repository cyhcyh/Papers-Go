import asyncio
import json

import pytest

from app.db import dumps, execute, one, rows
from app.llm import runtime
from app.llm.provider import cloud
from app.pipeline import tldr
from app.pipeline_control import cancellation_scope


FORMULA = r'$\frac{q^{n}-1}{q-1}\leq N$'
BRIEF = {'title_zh':'论文译名', 'problem':'研究给定条件下的精确上界',
         'contribution_result':'在给定条件下证明 '+FORMULA+'。'}


def config(kind='cloud', fallback=False):
    value = runtime.legacy_defaults()
    value['connections'][1].update(kind=kind, api_key='test-key')
    value['routes']['brief'] = {
        'primary':{'connection_id':'cloud', 'model':'primary', 'thinking':'auto'},
        'fallback':{'connection_id':'cloud', 'model':'backup'} if fallback else None,
    }
    return value


def save_old(ident):
    execute('UPDATE papers SET brief_json=?,tldr=? WHERE id=?', (dumps(BRIEF), '旧速读', ident))
    return one('SELECT brief_json,tldr FROM papers WHERE id=?', (ident,))


def test_brief_normalizes_whitespace_before_length_validation_and_keeps_latex():
    text = '结论 '+FORMULA*24
    assert 600 < len(text) < 1200
    value = tldr.validate_brief({**BRIEF, 'contribution_result':'\n  '+text+'  \n'*400})
    assert value.contribution_result == text
    assert tldr.PaperBrief.model_json_schema()['properties']['contribution_result']['maxLength'] == 1200


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cloud', 'ollama', 'codex'])
async def test_valid_brief_needs_one_request_for_every_provider(client, papers, monkeypatch, kind):
    calls = []
    result = {**BRIEF, 'contribution_result':'结论 '+FORMULA*24}
    async def request(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    provider = cloud
    method = 'complete'
    if kind == 'ollama':
        from app.llm.ollama import ollama
        provider, method = ollama, 'chat'
    elif kind == 'codex':
        from app.llm.codex import codex
        provider = codex
    monkeypatch.setattr(provider, method, request)
    with runtime.model_snapshot(config(kind), replace=True):
        assert await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],))) == result
    assert len(calls) == 1
    assert json.loads(one('SELECT brief_json FROM papers WHERE id=?', (papers[0],))['brief_json']) == result


@pytest.mark.asyncio
@pytest.mark.parametrize('field,limit', [('problem',300), ('contribution_result',1200)])
async def test_only_overlong_brief_is_compacted_once_with_source_and_original_prompt(client, papers, monkeypatch, field, limit):
    custom = '管理员自定义速读要求，保留必要条件和完整公式。'
    execute("INSERT INTO app_settings(name,value,updated_at) VALUES('prompts',?,'test')", (dumps({'brief':custom}),))
    overlong = {**BRIEF, field:'说明'*limit}
    calls = []
    async def request(messages, **kwargs):
        calls.append(messages)
        return overlong if len(calls) == 1 else dict(BRIEF)
    monkeypatch.setattr(cloud, 'complete', request)
    paper = one('SELECT * FROM papers WHERE id=?', (papers[0],))
    with runtime.model_snapshot(config(), replace=True):
        assert await tldr.generate_brief(paper) == BRIEF
    assert len(calls) == 2
    assert calls[0][0]['content'] == custom
    assert calls[1][:2] == calls[0]
    assert json.loads(calls[1][2]['content']) == overlong
    assert str(limit) in calls[1][3]['content']
    assert one("SELECT value FROM app_settings WHERE name='prompts'")['value'] == dumps({'brief':custom})
    saved = json.loads(one('SELECT brief_json FROM papers WHERE id=?', (papers[0],))['brief_json'])
    assert saved == BRIEF and FORMULA in saved['contribution_result']
    log = rows("SELECT detail FROM app_logs WHERE message='论文速读超长，尝试精简一次'")
    assert json.loads(log[0]['detail']) == {'paper_id':papers[0], 'fields':[{'field':field, 'length':limit*2, 'limit':limit}]}


@pytest.mark.asyncio
async def test_failed_compaction_keeps_old_brief_and_reports_lengths_without_output(client, papers, monkeypatch):
    before = save_old(papers[0])
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return {**BRIEF, 'contribution_result':'长'*1201}
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(config(), replace=True), pytest.raises(tldr.BriefLengthError, match='主要贡献和结果 1201 / 1200 字符'):
        await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls) == 2
    assert one('SELECT brief_json,tldr FROM papers WHERE id=?', (papers[0],)) == before
    logs = rows("SELECT detail FROM app_logs WHERE level='error' AND job IN ('brief','tldr_gen')")
    assert logs and all('长'*10 not in log['detail'] for log in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'title_zh':'缺少字段'},
    {**BRIEF, 'problem':'研究问题'},
    {**BRIEF, 'problem':None},
    {**BRIEF, 'problem':'', 'contribution_result':'长'*1201},
])
async def test_non_length_errors_do_not_trigger_compaction(client, papers, monkeypatch, payload):
    before = save_old(papers[0])
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return payload
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(config(), replace=True), pytest.raises(ValueError):
        await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls) == 1
    assert one('SELECT brief_json,tldr FROM papers WHERE id=?', (papers[0],)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_fails', [False, True])
async def test_primary_and_fallback_share_one_extra_compaction_request(client, papers, monkeypatch, primary_fails):
    calls = []
    async def request(*args, **kwargs):
        calls.append(runtime.current_binding()['model'])
        if primary_fails and len(calls) == 1:
            raise ValueError('主模型服务失败')
        if primary_fails and len(calls) == 3:
            return dict(BRIEF)
        return {**BRIEF, 'contribution_result':'长'*1201}
    monkeypatch.setattr(cloud, 'complete', request)
    before = save_old(papers[0])
    with runtime.model_snapshot(config(fallback=True), replace=True):
        if primary_fails:
            assert await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],))) == BRIEF
        else:
            with pytest.raises(tldr.BriefLengthError):
                await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
            assert one('SELECT brief_json,tldr FROM papers WHERE id=?', (papers[0],)) == before
    assert calls == (['primary','backup','backup'] if primary_fails else ['primary','primary','backup'])


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_at', [1, 2])
async def test_stop_prevents_extra_request_or_publishing_compacted_result(client, papers, monkeypatch, stop_at):
    before = save_old(papers[0])
    stopped = asyncio.Event()
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        if len(calls) == stop_at:
            stopped.set()
        return {**BRIEF, 'problem':'长'*301} if len(calls) == 1 else dict(BRIEF)
    monkeypatch.setattr(cloud, 'complete', request)
    with cancellation_scope(stopped), runtime.model_snapshot(config(), replace=True), pytest.raises(asyncio.CancelledError):
        await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls) == stop_at
    assert one('SELECT brief_json,tldr FROM papers WHERE id=?', (papers[0],)) == before


@pytest.mark.asyncio
async def test_other_features_keep_existing_validation_behavior(client, monkeypatch):
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return {**BRIEF, 'problem':'长'*301}
    monkeypatch.setattr(cloud, 'complete', request)
    value = config()
    value['routes']['quality'] = value['routes']['brief']
    with runtime.model_snapshot(value, replace=True), pytest.raises(ValueError):
        await runtime.complete('quality', [], validate=tldr.PaperBrief.model_validate)
    assert len(calls) == 1
