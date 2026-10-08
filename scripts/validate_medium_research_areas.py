"""Independent Docker-model experiment; never changes production labels or config."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIMARY = {
    502: ['Computer Graphics', 'Generative Modeling'],
    742: ['Robotics'],
    1170: ['Large Language Models', 'Explainable AI'],
    1319: ['Reinforcement Learning'],
    1427: ['Large Language Models', 'Explainable AI'],
    1523: ['Computer Security', 'Adversarial Machine Learning'],
    1593: ['Reinforcement Learning', 'Automated Planning'],
    1666: ['Large Language Models', 'Explainable AI'],
    2314: ['Representation Learning', 'Deep Learning'],
    2485: ['Distributed Systems', 'High-Performance Computing', 'Federated Learning'],
    2848: ['Natural Language Processing', 'Information Retrieval'],
    2963: ['High-Performance Computing'],
    3129: ['Federated Learning', 'Distributed Systems'],
    3457: ['Large Language Models', 'Natural Language Processing'],
    3494: ['Bayesian Statistics', 'Machine Learning'],
    3760: ['Representation Learning', 'Natural Language Processing'],
    3996: ['Zero-Shot Learning'],
    4005: ['Large Language Models', 'Natural Language Processing'],
    4210: ['Robotics', 'Multimodal Machine Learning'],
    4406: ['Computational Statistics', 'Multimodal Machine Learning', 'Large Language Models'],
    4723: ['Computer Graphics', 'Generative Modeling'],
    5037: ['Robotics', 'Large Language Models'],
    5277: ['Enumerative Combinatorics', 'Algebraic Combinatorics'],
    5278: ['Design Theory'],
    5284: ['Extremal Graph Theory'],
    5286: ['Enumerative Combinatorics'],
    5368: ['Extremal Graph Theory'],
    5392: ['Enumerative Combinatorics', 'Algebraic Combinatorics'],
    5488: ['Additive Combinatorics'],
    5551: ['Algebraic Combinatorics'],
    11071: ['Representation Learning', 'Explainable AI', 'Natural Language Processing', 'Large Language Models'],
    14911: ['Domain Adaptation', 'Multimodal Machine Learning'],
    22131: ['High-Performance Computing'],
    27415: ['Representation Learning', 'Machine Learning'],
}
RELATED = {
    502: ['Computer Vision', 'Deep Learning'], 742: ['Large Language Models'],
    1170: ['Adversarial Machine Learning', 'Machine Learning'],
    1319: ['Large Language Models', 'Continual Learning'],
    1427: ['Natural Language Processing', 'Machine Learning'],
    1523: ['Large Language Models'], 1593: ['Robotics'],
    1666: ['Natural Language Processing', 'Machine Learning'],
    2314: ['Machine Learning'], 2485: ['Machine Learning', 'Online Algorithms'],
    2848: ['Large Language Models'], 2963: ['Large Language Models', 'Computer Architecture'],
    3129: ['Software Architecture', 'Machine Learning'], 3457: ['Deep Learning'],
    3494: ['Large Language Models', 'Recommender Systems'], 3760: ['Large Language Models'],
    3996: ['Machine Learning', 'Large Language Models'],
    4005: ['Active Learning', 'Automated Machine Learning'], 4210: ['Computer Vision'],
    4406: ['Machine Learning', 'Randomized Algorithms'], 4723: ['Computer Vision', 'Deep Learning'],
    5037: ['Software Testing'], 5277: [], 5278: ['Algebraic Combinatorics'],
    5284: ['Structural Graph Theory'], 5286: [], 5368: ['Structural Graph Theory'],
    5392: ['Algebraic Geometry'], 5488: [], 5551: ['Representation Theory'],
    11071: [], 14911: ['Machine Learning', 'Natural Language Processing'],
    22131: ['Deep Learning'], 27415: ['Generative Modeling', 'Deep Learning'],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--taxonomy', default=r'D:\Python_project\学科分类表\taxonomy\research_areas.json')
    parser.add_argument('--sample', default=str(ROOT / 'artifacts/classification-final-blind-results-20261003.json'))
    parser.add_argument('--output', default=str(ROOT / 'artifacts/classification-medium-areas-results-20261003.json'))
    parser.add_argument('--container', default='shualunwen-app-1')
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    areas = json.loads(Path(args.taxonomy).read_text(encoding='utf-8-sig'))
    if not isinstance(areas, list) or not areas:
        raise ValueError('Taxonomy must be a nonempty list.')
    for area in areas:
        if set(area) != {'id', 'discipline', 'name', 'description'} or not all(isinstance(v, str) and v.strip() for v in area.values()):
            raise ValueError('Invalid taxonomy row.')
        if area['discipline'] not in ['Mathematics', 'Computer Science']:
            raise ValueError('Unsupported discipline in this experiment.')
    if len({area['id'] for area in areas}) != len(areas) or len({area['name'].casefold() for area in areas}) != len(areas):
        raise ValueError('Duplicate IDs or names.')
    sample = json.loads(Path(args.sample).read_text(encoding='utf-8'))
    papers = [{
        'id': row['id'], 'title': row['title'], 'abstract': row['abstract'],
        'primary_category': row['category'], 'venue': row['venue'], 'categories': '[]',
    } for row in sample['results']]
    assert {paper['id'] for paper in papers} == set(PRIMARY) == set(RELATED)
    by_name = {area['name']: area for area in areas}
    assert all(name in by_name for names in list(PRIMARY.values()) + list(RELATED.values()) for name in names)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Freeze descriptive targets before making any model requests. Multiple
    # acceptable primary directions accommodate overlap in this flat taxonomy.
    reference = {'scope': '同34篇开发样本的阅读参考，非外部专家金标准；primary与related分开统计。', 'cases': [{
        'id': paper['id'], 'title': paper['title'],
        'primary_names': PRIMARY[paper['id']], 'primary_ids': [by_name[name]['id'] for name in PRIMARY[paper['id']]],
        'related_names': RELATED[paper['id']], 'related_ids': [by_name[name]['id'] for name in RELATED[paper['id']]],
    } for paper in papers]}
    reference_path = output.with_suffix('.reference.json')
    if reference_path.exists() and json.loads(reference_path.read_text(encoding='utf-8')) != reference:
        raise ValueError('Existing frozen reference differs; do not overwrite after observing results.')
    reference_path.write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding='utf-8')
    output.with_suffix('.taxonomy.json').write_text(json.dumps(areas, ensure_ascii=False, indent=2), encoding='utf-8')
    payload = {'areas': areas, 'papers': papers, 'reference': reference, 'concurrency': args.concurrency,
               'taxonomy_source': str(Path(args.taxonomy).resolve()), 'baseline_summary': sample['summary']}
    log_path = output.with_suffix('.jsonl')
    if args.resume and log_path.exists():
        completed = {}
        for line in log_path.read_text(encoding='utf-8').splitlines():
            if line.startswith('PAPER_JSON:'):
                row = json.loads(line[len('PAPER_JSON:'):])
                if row.get('ok'):
                    completed[row['id']] = row
        payload['prior_results'] = list(completed.values())
        payload['papers'] = [paper for paper in papers if paper['id'] not in completed]
        print(json.dumps({'resume_completed': len(completed), 'remaining': len(payload['papers'])}), flush=True)
    worker = (ROOT / 'scripts/medium_research_areas_worker.py').read_text(encoding='utf-8')
    process = subprocess.Popen(['docker', 'exec', '-i', args.container, 'python', '-B', '-c', worker],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    process.stdin.close()
    report = None
    with log_path.open('a' if args.resume else 'w', encoding='utf-8') as log:
        for data in iter(process.stdout.readline, b''):
            line = data.decode('utf-8', errors='replace').rstrip()
            log.write(line + '\n'); log.flush()
            if line.startswith('RESULT_JSON:'):
                report = json.loads(line[len('RESULT_JSON:'):])
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'report': str(output), 'summary': report['summary']}, ensure_ascii=False), flush=True)
            elif line.startswith('PAPER_JSON:'):
                row = json.loads(line[len('PAPER_JSON:'):])
                print(json.dumps({'completed_paper': row['id'], 'ok': row['ok'],
                    'first': row.get('first_name'), 'final': row.get('final_name'),
                    'review': row.get('decision', {}).get('needs_review'), 'seconds': row['seconds']}, ensure_ascii=False), flush=True)
            else:
                print(line, flush=True)
    code = process.wait()
    if code or report is None:
        raise RuntimeError(f'Experiment process failed ({code}); see {output.with_suffix(".jsonl")}')


if __name__ == '__main__':
    main()
