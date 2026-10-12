"""One model-assisted JSON correction, explicitly enabled by each task."""
import json

from .. import prompts
from ..db import dumps


def repair_messages(messages, result, error, schema):
    if not isinstance(error, json.JSONDecodeError):
        return None
    detail = f'{error.msg}，第 {error.lineno} 行，第 {error.colno} 列，字符位置 {error.pos}'
    return [*messages, *([{'role':'assistant', 'content':error.doc[:24000]}] if error.doc else []),
            {'role':'user', 'content':prompts.get('json_repair')+'\n解析错误：'+detail
             +'\n输出结构与长度限制：'+dumps(schema)}]
