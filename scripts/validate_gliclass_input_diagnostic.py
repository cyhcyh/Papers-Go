"""Small local checks for input truncation and label-menu sensitivity."""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'artifacts/gliclass-runtime'))
os.environ['HF_HOME'] = str(ROOT / 'artifacts/gliclass-hf-cache')
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
sys.stdout.reconfigure(encoding='utf-8')

import torch
from gliclass import GLiClassModel, ZeroShotClassificationPipeline
from transformers import AutoTokenizer

torch.set_num_threads(6)
torch.set_num_interop_threads(1)
weights = ROOT / 'artifacts/gliclass-model'
model = GLiClassModel.from_pretrained(weights, local_files_only=True)
tokenizer = AutoTokenizer.from_pretrained(weights, local_files_only=True)
pipe = ZeroShotClassificationPipeline(model, tokenizer, device='cpu',
    classification_type='multi-label', max_length=1536, progress_bar=False)
assert tokenizer.convert_tokens_to_ids('<<LABEL>>') == model.config.class_token_index
assert tokenizer.convert_tokens_to_ids('<<SEP>>') == model.config.text_token_index
catalog = json.loads((ROOT / 'backend/app/resources/standard_catalog.json').read_text(encoding='utf-8'))
papers = json.loads((ROOT / 'artifacts/classification-gliclass-coarse-results-20261003.json').read_text(encoding='utf-8'))['results']

def rank(text, labels):
    return sorted(pipe(text, labels, threshold=0.0)[0], key=lambda x: -x['score'])

rows = []
started = time.perf_counter()
for paper in papers:
    if paper['id'] not in (5284, 5278, 5551, 5488, 2314):
        continue
    labels = [e['label'] for e in catalog if not e['parent'] and e['system'] == paper['system']]
    text = paper['title'] + '\n' + paper['abstract']
    inputs = tokenizer(pipe.pipe.prepare_input(text, labels))['input_ids']
    truncated = inputs[:1535] + [inputs[-1]] if len(inputs) > 1536 else inputs
    chunked = []
    for offset in range(0, len(labels), 16):
        chunked.extend(rank(text, labels[offset:offset + 16]))
    chunked.sort(key=lambda x: -x['score'])
    simple_labels = ['Combinatorics', 'Graph theory', 'Number theory', 'Algebraic topology',
                     'Quantum physics', 'Machine learning', 'K-theory', 'Special functions']
    row = {'id': paper['id'], 'title': paper['title'], 'input_tokens': len(inputs),
           'label_markers_expected': len(labels),
           'label_markers_present': truncated.count(model.config.class_token_index),
           'text_marker_present': truncated.count(model.config.text_token_index),
           'original_top': paper['root_top'], 'chunks_16_top': chunked[:5],
           'simple_menu_top': rank(text, simple_labels)[:4]}
    rows.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
result = {'summary': {'samples': len(rows), 'seconds': round(time.perf_counter() - started, 3),
                      'device': 'cpu', 'special_tokens_match': True}, 'results': rows}
(ROOT / 'artifacts/classification-gliclass-input-diagnostic-20261003.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result['summary']), flush=True)
