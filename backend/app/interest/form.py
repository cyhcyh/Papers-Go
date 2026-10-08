"""Round trip the versioned research profile through ordinary form fields."""
import re
from datetime import date
from pydantic import BaseModel, Field, field_validator


class InterestEntry(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    weight: float = Field(.7, ge=0, le=1)
    until: date | None = None

    @field_validator('text')
    @classmethod
    def clean_text(cls, value):
        value = ' '.join(value.split())
        if not value:
            raise ValueError('兴趣内容不能为空')
        return value


class ProfileForm(BaseModel):
    description: str = Field('', max_length=2000)
    long_term: list[InterestEntry] = Field(default_factory=list, max_length=40)
    temporary: list[InterestEntry] = Field(default_factory=list, max_length=30)
    exclusions: list[str] = Field(default_factory=list, max_length=30)

    @field_validator('exclusions')
    @classmethod
    def clean_exclusions(cls, values):
        cleaned = [' '.join(v.split()) for v in values if v.strip()]
        if any(len(v) > 500 for v in cleaned):
            raise ValueError('排除方向过长')
        return cleaned


def section_kind(heading):
    if '排除' in heading:
        return 'exclusions'
    if any(label in heading for label in ('近期关注', '阶段', '临时')):
        return 'temporary'
    if '系统' in heading:
        return 'inferred'
    if '研究方向描述' in heading:
        return 'description'
    return 'long_term'


def without_background(content):
    lines, section = [], 'long_term'
    for line in content.splitlines():
        if line.startswith('##'):
            section = section_kind(line)
        if section != 'description':
            lines.append(line)
    return '\n'.join(lines)


def normalize_recent_name(content):
    def heading(match):
        text = match[0]
        if section_kind(text) != 'temporary':
            return text
        return '## 近期关注' + ('\r' if text.endswith('\r') else '')
    return re.sub(r'^##[^\n]*', heading, content, flags=re.M)


def parse_form(content):
    result = {'description': '', 'long_term': [], 'temporary': [], 'exclusions': [], 'inferred': []}
    section = 'long_term'
    for line in content.splitlines():
        if line.startswith('##'):
            section = section_kind(line)
            continue
        text = line.strip()
        if not text or text.startswith('#'):
            continue
        text = re.sub(r'^[-*]\s+', '', text)
        weight = re.search(r'w:([\d.]+)', text)
        expiry = re.search(r'until:(\d{4}-\d{2}-\d{2})', text)
        text = re.sub(r'\[w:[^]]*\]', '', text).strip()
        if section == 'description':
            result['description'] += ('\n' if result['description'] else '') + text
        elif section in ('exclusions', 'inferred'):
            result[section].append(text)
        elif text:
            result[section].append({'text': text, 'weight': min(1, float(weight[1])) if weight else .7,
                                    'until': expiry[1] if expiry else None})
    return result


def render_form(form, previous=''):
    lines = ['## 研究方向描述']
    if form.description.strip():
        lines.extend('- ' + line.strip() for line in form.description.splitlines() if line.strip())
    for label, entries in [('核心兴趣（长期）', form.long_term), ('近期关注', form.temporary)]:
        lines += ['', '## ' + label]
        for entry in entries:
            metadata = f'w:{entry.weight:g}' + (f', until:{entry.until.isoformat()}' if entry.until else '')
            lines.append(f'- [{metadata}] {entry.text}')
    lines += ['', '## 明确排除', *('- ' + item for item in form.exclusions)]
    # Preserve model-inferred entries including their weights and expiry dates.
    inferred = re.search(r'^##[^\n]*系统[^\n]*\n(.*?)(?=^##|\Z)', previous, re.M | re.S)
    lines += ['', '## 系统推断（每周反思更新）', inferred[1].strip() if inferred else '']
    return '\n'.join(lines)
