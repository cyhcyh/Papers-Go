"""Seed an isolated UI preview. Never writes to the production data directory."""
import os
from pathlib import Path

os.environ['DATA_DIR'] = str(Path('data/ui-redesign').resolve())
os.environ['SCHEDULER_ENABLED'] = 'false'
os.environ['BOOTSTRAP_ENABLED'] = 'false'
from app.db import init_db, execute, one, dumps, connect
from app.standard_topics import catalog, queue_or_assign, approve
from app.config import now, today
from app.auth import hash_password
from app.interest.profile import put_profile, current

init_db()
if not one('SELECT id FROM users LIMIT 1'):
    user_id = execute('INSERT INTO users(username,password_hash,is_admin,created_at) VALUES(?,?,1,?)',
                      ('ui_preview', hash_password('preview-local-2026'), now()))


examples = [
    ('智能体如何在长期任务中记住关键线索？', 'Graph Memory for Long-Horizon Agents', 'cs.AI', None, 3,
     '研究智能体在长程任务中检索和更新历史信息的问题。', '通过图结构组织任务记忆，将检索与记忆更新结合。', '此为界面演示论文，主要结果位置展示摘要中可核实的结论。'),
    ('从局部结构出发，探索 Ramsey 问题的新界', 'Local Structure and Ramsey Bounds', 'math.CO', None, 15,
     '关注图的局部结构如何约束全局染色与 Ramsey 性质。', '结合概率方法与结构分析，构建新的证明思路。', '此为界面演示论文；真实论文会在这里展示定理或上下界。'),
    ('让检索增强模型知道什么时候需要检索', 'Adaptive Retrieval for Language Models', 'cs.CL', 'ICLR.2026', 8,
     '研究语言模型在回答问题时如何判断外部知识的需求。', '将检索时机的判断加入生成过程，按需补充相关材料。', '此为界面演示论文；此处显示摘要明确报告的主要发现。'),
    ('长程任务的评测，应该怎样设计？', 'Evaluating Agents Beyond Single Tasks', 'cs.MA', 'NeurIPS.2026', 4,
     '研究现有短任务评测能否反映智能体的长期能力。', '设计覆盖多阶段任务、工具使用与记忆更新的评测框架。', '此为界面演示论文，实际结果会依据论文摘要生成。'),
    ('在持续学习中保留过去的经验', 'Learning Without Forgetting Past Tasks', 'cs.LG', 'ICML.2026', 10,
     '关注学习新任务时对已有能力造成的影响。', '使用经验重放与参数约束协调新旧任务。', '此为界面演示论文，结果字段不包含未验证的提升数字。'),
    ('视觉与语言如何共享同一份记忆？', 'Shared Memory for Vision and Language', 'cs.CV', 'AAAI.2026', 11,
     '研究图像与语言信息在跨模态任务中的关联。', '通过统一表示组织视觉线索和文字描述。', '此为界面演示论文，真实内容以摘要解读为准。'),
    ('从证明步骤中学习组合问题的结构', 'Learning Structures in Combinatorial Proofs', 'math.CO', None, 14,
     '研究组合证明中可复用的结构与步骤。', '将证明拆解为可检查的构造与推导过程。', '此为界面演示论文，摘要未说明的结果会明确标注。'),
    ('多智能体如何协作规划复杂任务', 'Cooperative Planning for Multi-Agent Systems', 'cs.MA', None, 5,
     '研究多个智能体共同完成任务时的协调问题。', '在共享计划中显式描述依赖、分工与工具调用。', '此为界面演示论文，供桌面和手机布局验证。'),
]
for i, (zh, en, category, venue, topic, problem, contribution, result) in enumerate(examples):
    if one('SELECT id FROM papers WHERE arxiv_id=?', (f'ui-demo:{i}',)):
        continue
    ident = execute('INSERT INTO papers(arxiv_id,title,authors,abstract,primary_category,categories,venue,published,created_at,ingested_date,tldr,brief_json,quality_score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f'ui-demo:{i}', en, dumps(['UI Preview Researcher']), 'UI preview only. This synthetic paper demonstrates the paper card layout and is not a research source.', category, dumps([category]), venue, today(), now(), today(), contribution,
                     dumps({'title_zh': '示例 · ' + zh, 'problem': problem, 'contribution': contribution, 'result': result}), 60 + i))
    labels = {3:'Intelligent agents',4:'Artificial intelligence',5:'Multi-agent planning',8:'Information retrieval',10:'Machine learning',11:'Computer vision',14:'Ramsey theory',15:'Ramsey theory'}
    entry = next(e for e in catalog().values() if e['label']==labels[topic])
    with connect() as db:
        topic_id,_ = queue_or_assign(db,one('SELECT * FROM papers WHERE id=?',(ident,)),entry,1,entry['name_zh'])
        approve(db,topic_id)
user_id=one('SELECT id FROM users ORDER BY id LIMIT 1')['id']
if not current(user_id):
    put_profile(user_id, '## 研究方向描述\n- [w:0.9] 智能体记忆与组合数学\n## 核心兴趣（长期）\n- [w:0.7] 长程任务评测\n## 阶段性关注（带 TTL）\n- [w:0.9, until:2099-01-01] Ramsey 理论\n## 明确排除\n- 机器人\n## 系统推断\n- [w:0.4] 图谱记忆',
                {'topic_ids': [], 'category_selection': {'categories': ['arxiv:math.CO','arxiv:cs.AI'], 'topics': {}}}, 'init')
print('Isolated preview ready: data/ui-redesign, 8 synthetic papers.')
