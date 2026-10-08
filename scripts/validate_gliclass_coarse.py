"""Read-only GLiClass coarse routing over the real standard directory."""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'artifacts/gliclass-runtime'))
os.environ.setdefault('HF_HOME', str(ROOT / 'artifacts/gliclass-hf-cache'))
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
sys.stdout.reconfigure(encoding='utf-8')


def eligible(entry):
    return bool(entry['parent']) and (entry['system'] != 'MSC2020' or
            '-' not in entry['code'] and not entry['code'].endswith('99'))


def main(args):
    import torch
    import transformers
    import gliclass
    from gliclass import GLiClassModel, ZeroShotClassificationPipeline
    from transformers import AutoTokenizer

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    entries = json.loads((ROOT / 'backend/app/resources/standard_catalog.json').read_text(encoding='utf-8'))
    by_key = {e['key']: e for e in entries}
    children = defaultdict(list)
    for entry in entries:
        children[entry['parent']].append(entry)

    def path(entry):
        labels = []
        while entry:
            labels.append(entry['label'])
            entry = by_key.get(entry['parent'])
        return ' › '.join(reversed(labels))

    def descendants(entry):
        found = [entry] if eligible(entry) else []
        for child in children[entry['key']]:
            found.extend(descendants(child))
        return found

    papers = json.loads(Path(args.sample).read_text(encoding='utf-8'))['results']
    print(json.dumps({'stage': 'loading', 'device': 'cpu', 'papers': len(papers)}), flush=True)
    load_start = time.perf_counter()
    model = GLiClassModel.from_pretrained(args.weights, local_files_only=True)
    tokenizer = AutoTokenizer.from_pretrained(args.weights, local_files_only=True)
    pipeline = ZeroShotClassificationPipeline(model, tokenizer, classification_type='multi-label',
                device='cpu', max_length=args.max_length, progress_bar=False)
    load_seconds = time.perf_counter() - load_start

    def rank(text, options):
        labels = list(dict.fromkeys(path(e) for e in options))
        result = pipeline(text, labels, threshold=0.0, batch_size=1)[0]
        return sorted(result, key=lambda item: -item['score'])

    warmup_start = time.perf_counter()
    rank('A study of machine learning algorithms.', [e for e in children[None] if e['system'] == 'CCS2012'])
    warmup_seconds = time.perf_counter() - warmup_start
    started = time.perf_counter()
    rows = []
    for paper in papers:
        row = {k: paper.get(k) for k in ('id', 'title', 'abstract', 'category', 'venue')}
        system = 'MSC2020' if (paper.get('category') or '').startswith('math.') else 'CCS2012'
        row['system'] = system
        before = time.perf_counter()
        text = paper['title'] + '\n' + (paper.get('abstract') or '')
        roots = [e for e in children[None] if e['system'] == system]
        root_scores = rank(text, roots)
        selected_roots = {x['label'] for x in root_scores[:args.top_k]}
        branches = [child for root in roots if path(root) in selected_roots
                    for child in children[root['key']] if eligible(child)]
        if not branches:
            branches = [root for root in roots if path(root) in selected_roots]
        branch_scores = rank(text, branches)
        selected_branches = {x['label'] for x in branch_scores[:args.top_k]}
        options = {}
        for branch in branches:
            if path(branch) in selected_branches:
                options.update({e['key']: e for e in descendants(branch)})
        row.update(root_top=[{'path': x['label'], 'score': round(x['score'], 6)} for x in root_scores[:5]],
                   branch_top=[{'path': x['label'], 'score': round(x['score'], 6)} for x in branch_scores[:6]],
                   candidates=[{'key': e['key'], 'code': e['code'], 'label': e['label'], 'path': path(e)} for e in options.values()],
                   candidate_count=len(options), coarse_seconds=round(time.perf_counter() - before, 3))
        if not row['candidates']:
            raise ValueError('No eligible real standard candidates for paper ' + str(paper['id']))
        rows.append(row)
        if len(rows) % 4 == 0 or len(rows) == len(papers):
            print(json.dumps({'stage': 'coarse', 'completed': len(rows), 'total': len(papers),
                              'elapsed_seconds': round(time.perf_counter() - started, 2)}), flush=True)

    summary = {'model': 'knowledgator/gliclass-large-v3.0',
               'revision': 'e065d1844f913a9aa611cf33623a9538b8aa8841',
               'device': 'cpu', 'dtype': str(next(model.parameters()).dtype),
               'torch': torch.__version__, 'transformers': transformers.__version__, 'gliclass': gliclass.__version__,
               'cpu_threads': args.threads, 'max_length': args.max_length, 'top_k_roots': args.top_k,
               'top_k_branches': args.top_k, 'local_forward_calls': len(rows) * 2,
               'load_seconds': round(load_seconds, 3), 'warmup_seconds': round(warmup_seconds, 3),
               'papers': len(rows), 'elapsed_seconds': round(time.perf_counter() - started, 3),
               'average_seconds': round(statistics.mean(r['coarse_seconds'] for r in rows), 3),
               'median_seconds': round(statistics.median(r['coarse_seconds'] for r in rows), 3),
               'average_candidates': round(statistics.mean(r['candidate_count'] for r in rows), 1),
               'maximum_candidates': max(r['candidate_count'] for r in rows)}
    Path(args.output).write_text(json.dumps({'summary': summary, 'results': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--weights', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--threads', type=int, default=6)
    parser.add_argument('--top-k', type=int, default=2)
    parser.add_argument('--max-length', type=int, default=1536)
    main(parser.parse_args())
