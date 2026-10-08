"""Summarize saved experimental results; makes no model calls or database writes."""
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts'


def read(name):
    return json.loads((ARTIFACTS / name).read_text(encoding='utf-8'))


initial = read('classification-cloud-reranker-hierarchy-results-20261003.json')
expanded = read('classification-cloud-reranker-expanded-results-20261003.json')
references = read('classification-reranker-reference-20261003.json')['references']
baseline = read('classification-final-blind-results-20261003.json')
catalog = json.loads((ROOT / 'backend/app/resources/standard_catalog.json').read_text(encoding='utf-8'))
by_key = {entry['key']: entry for entry in catalog}
initial_rows = {row['id']: row for row in initial['results']}
expanded_rows = {row['id']: row for row in expanded['results']}
final_rows = initial_rows | expanded_rows
attempts = initial['results'] + expanded['results']
assert set(expanded_rows) == {row['id'] for row in initial['results'] if row.get('final_key') is None}
assert all(row['ok'] and row['final_key'] in by_key for row in final_rows.values())

# Reading-based judgments of the title, abstract and complete selected path.
# These are development-sample judgments, not an independent expert gold set.
review_groups = {
    'main_direction_fit': [1319, 1523, 1593, 2485, 3129, 4210, 4723, 5277, 5284, 5286, 5368, 5392, 5551, 22131, 27415],
    'related_but_broad': [502, 3457, 11071],
    'related_but_core_or_scope_problem': [742, 1666, 2963, 3494, 3996, 4005, 4406, 5037, 5488, 14911],
    'clearly_unsuitable': [1170, 1427, 2314, 2848, 3760, 5278],
}
notes = {
    502: '渲染是核心任务；Rendering已进入候选，但最终选了更泛的神经网络。',
    742: '机器人规划相关，但核心是基础模型演示采集及策略蒸馏，规划标签不能完整说明贡献。',
    1170: '比较LLM安全控制与监测方法，却选成通用Evaluation，混淆研究形式与主题。',
    1319: '主要是LLM强化学习后训练，强化学习方法路径符合。',
    1427: 'LLM诚实报告行为及表示干预的实证研究，不是计算理论中的Models of learning。',
    1523: '间接提示注入攻击属于软件与应用安全，方向符合。',
    1593: '离线目标条件强化学习中的变长分层规划，抽象规划方向可接受。',
    1666: '计算机使用智能体的惯性行为与干预，被归为泛控制方法；智能体条目未进入候选。',
    2314: 'SPD流形轨迹的功能神经网络架构，不属于计算理论的Models of learning。',
    2485: '客户端分配使用整数网络流优化，网络优化描述核心方法。',
    2848: '共形事实性控制用于RAG，被选成通用Evaluation，丢失研究主题。',
    2963: '智能体推理系统/GPU性能评测，被选成NLP；Software performance未进入候选。',
    3129: '贡献为联邦学习平台互操作性，Interoperability方向符合。',
    3457: '投机解码训练中的监督恢复，监督学习相关但粒度偏粗。',
    3494: '贝叶斯后验推断方向相关，但最终取ML理论路径；Bayesian computation也在候选中。',
    3760: '文本嵌入条件转向方法，被归入通用Experimentation，误将实验形式作为主题。',
    3996: '基于LLM先验的零标签表格分类方法，ML theory过泛且理论路径缺乏支持。',
    4005: '提示反思优化的自适应反馈分配，与样本选择相关，但不等同典型主动学习。',
    4210: '自动驾驶VLA的语义推理和轨迹仲裁，认知机器人方向可接受。',
    4406: '主要采样方法为顺序蒙特卡洛，被归为泛知识表示与推理；SMC方向未进入候选。',
    4723: '视频虚拟试穿的纹理保持，扩展回退后外观与纹理表示方向可接受。',
    5037: '编码智能体的机器人开发流程基准，被过度收窄为机器人规划。',
    5277: '反链多项式的枚举与双射，05A19合理；冻结参考未列全这一可接受条目。',
    5278: '正交量子拉丁方的设计构造，有限阿贝尔群是工具，不能替代主要设计主题。',
    5284: 'Turán型极值图问题，05C35符合。',
    5286: '排列模式及Wilf等价，05A05符合。',
    5368: '图的顶点packing数下界，05C35可接受，05C69也可接受，不能混作边分解05C70。',
    5392: '阶梯图与光滑排列模式避免/枚举，05A05可接受。',
    5488: '和集/差集不等式属于加性组合，但Inverse problems标签过窄且摘要未支持逆问题；更泛组合不等式候选也可用。',
    5551: 'Kromatic对称函数展开，05E05符合。',
    11071: 'LLM内部句法与语义编码，NLP领域正确但较粗。',
    14911: '测试时多模态情感分析的方法研究，最终ML theory过泛且未对应核心方法。',
    22131: 'GPU并行共享内存算法优化，共享内存算法方向符合。',
    27415: '变长集合的连续场预测，结构化预测方向可接受。',
}
judgments = {paper_id: group for group, paper_ids in review_groups.items() for paper_id in paper_ids}
assert len(judgments) == 34 and set(judgments) == set(final_rows) == set(notes)


def root_of(key):
    entry = by_key[key]
    while entry['parent']:
        entry = by_key[entry['parent']]
    return entry['key']


def reference_coverage(rows, limit=None, roots=False):
    result = []
    for ref in references:
        row = rows[ref['id']]
        candidates = row['root_top'] if roots else row['candidates']
        if limit is not None:
            candidates = candidates[:limit]
        available = {candidate['key'] for candidate in candidates}
        expected = {root_of(key) if roots else key for key in ref['keys']}
        result.append({'id': ref['id'], 'present': bool(available & expected), 'reference_keys': ref['keys']})
    return {'present': sum(item['present'] for item in result), 'total': len(result), 'cases': result}


rerank_tokens = sum(request['usage'].get('total_tokens', 0) for row in attempts for request in row['rerank_requests'])
deepseek_tokens = sum(row['usage']['total_tokens'] for row in attempts)
rerank_calls = sum(len(row['rerank_requests']) for row in attempts)
seconds = sum(row['seconds'] for row in attempts)
results = []
for paper_id, row in sorted(final_rows.items()):
    paper_attempts = [initial_rows[paper_id]] + ([expanded_rows[paper_id]] if paper_id in expanded_rows else [])
    results.append({
        'id': paper_id, 'title': row['title'], 'system': row['system'],
        'final_key': row['final_key'], 'final_label': row['final_label'], 'final_path': row['final_path'],
        'manual_review': judgments[paper_id], 'manual_note': notes[paper_id],
        'attempts': len(paper_attempts),
        'cumulative_seconds': round(sum(attempt['seconds'] for attempt in paper_attempts), 3),
        'deepseek_tokens': sum(attempt['usage']['total_tokens'] for attempt in paper_attempts),
        'rerank_tokens': sum(request['usage'].get('total_tokens', 0) for attempt in paper_attempts for request in attempt['rerank_requests']),
    })

report = {
    'scope': '只读生产数据的开发样本实验；未更改应用代码、数据库标签、模型配置或Docker部署。',
    'limitations': [
        '同一批34篇开发样本，非独立留出测试，逐篇判断由本次阅读标题摘要作出，并非专家金标准。',
        '百炼qwen3-rerank未公开对应开源模型尺寸，不能把结果当作Qwen3-Reranker-0.6B/4B/8B的结果。',
        '19篇冻结参考的精确条目覆盖不等于分类准确率；其他合理标准条目可能未列入参考。',
        'BGE基线与本实验的提示词、候选组织、审核调用次数不同，耗时仅作观测比较。',
        '回退只对首轮无适用候选的4篇触发；已输出条目但分错的论文不会被这一规则自动修正。',
        'rerank与生成模型Token按不同产品计费，未混用单价估算金额。',
    ],
    'experiment': {
        'reranker': '百炼云端 qwen3-rerank（公开模型尺寸不明）',
        'final_model': 'deepseek-v4.1-flash', 'thinking': 'off', 'concurrency': 4,
        'first_pass': {'root_beam': 3, 'branch_beam': 5, 'fine_beam': 8, 'broader_alternatives': 2},
        'fallback': {'trigger': '首轮choice=0', 'root_beam': 6, 'branch_beam': 10, 'fine_beam': 12, 'broader_alternatives': 4},
        'primary_topics_per_paper': 1,
    },
    'first_pass_summary': initial['summary'],
    'fallback_summary': expanded['summary'],
    'combined_summary': {
        'unique_papers': len(final_rows), 'assigned': len(final_rows), 'failed': 0,
        'rerank_calls': rerank_calls,
        'rerank_document_pairs': sum(request['documents'] for row in attempts for request in row['rerank_requests']),
        'rerank_total_tokens': rerank_tokens, 'average_rerank_tokens_per_paper': round(rerank_tokens / len(final_rows), 1),
        'deepseek_calls': len(attempts), 'deepseek_total_tokens': deepseek_tokens,
        'average_deepseek_tokens_per_paper': round(deepseek_tokens / len(final_rows), 1),
        'average_cumulative_seconds_per_paper': round(seconds / len(final_rows), 3),
        'two_batches_total_inference_wall_seconds': round(initial['summary']['batch_seconds'] + expanded['summary']['batch_seconds'], 3),
    },
    'reference_candidate_coverage': {
        'reference_scope': '实验前冻结的19篇指定主方向参考，参考条目非穷尽；只测是否进入候选，不测最终正确率。',
        'rerank_first_pass_top3_roots': reference_coverage(initial_rows, roots=True),
        'rerank_first_pass_all_candidates': reference_coverage(initial_rows),
        'rerank_after_fallback_all_candidates': reference_coverage(final_rows),
        'bge_baseline_first10': reference_coverage({row['id']: row for row in baseline['results']}, limit=10),
        'bge_baseline_all40': reference_coverage({row['id']: row for row in baseline['results']}),
    },
    'manual_review': {
        'category_counts': dict(Counter(judgments.values())),
        'msc8_category_counts': dict(Counter(row['manual_review'] for row in results if row['system'] == 'MSC2020')),
        'ccs26_category_counts': dict(Counter(row['manual_review'] for row in results if row['system'] == 'CCS2012')),
        'interpretation': '有标准条目不代表分对；重点失败为分层召回漏掉主方向，以及把理论/工具/评估形式错当主题。',
    },
    'conclusion': '平均耗时可接受，数学较GLiClass路线改善，但候选覆盖和AI路径判断未证明优于原BGE路线，暂不替换生产方案。',
    'results': results,
}
output = ARTIFACTS / 'classification-cloud-reranker-review-20261003.json'
output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({
    'report': str(output), 'combined_summary': report['combined_summary'],
    'coverage': {key: {'present': value['present'], 'total': value['total']} for key, value in report['reference_candidate_coverage'].items() if isinstance(value, dict)},
    'manual_review': report['manual_review'],
}, ensure_ascii=False))
