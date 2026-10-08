"""Manual execution shares exactly the scheduled pipeline stages."""
import argparse
import asyncio
from .db import init_db, rows, one
from .scheduler import jobs, run_job, pipeline


def main():
    parser=argparse.ArgumentParser(description='刷论文流水线')
    parser.add_argument('command',choices=['pipeline','status',*jobs])
    parser.add_argument('--limit',type=int,help='仅用于试运行的 arXiv 数量上限；省略时完整增量同步')
    args=parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit 必须大于 0')
    init_db()
    if args.command=='status':
        print('papers:',one('SELECT COUNT(*) AS n FROM papers')['n'])
        for source in rows('SELECT * FROM source_status'):
            print(source['name'],source['last_success'] or '-',source['error'] or 'ok')
    else:
        asyncio.run(pipeline(fetch_limit=args.limit) if args.command=='pipeline' else run_job(args.command, fetch_limit=args.limit))
        errors=rows('SELECT name,error FROM source_status WHERE error IS NOT NULL')
        for row in errors:
            print(row['name']+': '+row['error'])
        if any(row['name']==args.command or args.command=='pipeline' for row in errors):
            raise SystemExit(1)


if __name__=='__main__': main()
