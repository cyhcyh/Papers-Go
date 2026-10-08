"""Build a reusable public-taxonomy cache in an isolated directory."""
import argparse
import asyncio
import json
from pathlib import Path
import time
from app.config import settings
from app.db import init_db
from app.llm import runtime
from app.topic_semantics import ensure_index


async def build(directory):
    config=runtime.configuration()
    settings().data_dir=Path(directory);settings().embedding_dim=config['embedding_dim']
    init_db(recover=False)
    start=time.perf_counter()
    with runtime.model_snapshot(config):
        count=await ensure_index()
    print(json.dumps({'entries':count,'seconds':round(time.perf_counter()-start,2)}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True)
    asyncio.run(build(parser.parse_args().directory))
