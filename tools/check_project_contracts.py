"""开发源的只读快检：名称一致性、当前文档引用及可选的本机安装。

不扫描录像、不运行 OCR、不构建分享包；历史快照不参与当前文档检查。
"""
import argparse
import ast
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def _field(text, key):
    match = re.search(rf'^\s*{re.escape(key)}:\s*(.+?)\s*$', text, re.MULTILINE)
    return match.group(1).strip('"\'') if match else None


def _headings(text):
    anchors, counts = set(), {}
    for heading in re.findall(r'^#{1,6}\s+(.+)', text, re.MULTILINE):
        slug = re.sub(r'[^\w\- ]', '', heading.strip().lower()).replace(' ', '-')
        n = counts.get(slug, 0)
        counts[slug] = n + 1
        anchors.add(f'{slug}-{n}' if n else slug)
    return anchors


def check(root=ROOT, user_dir=None):
    root = Path(root).resolve()
    issues, texts = [], {}
    counts = {'identity_fields': 0, 'document_links': 0, 'source_pointers': 0}

    def issue(code, path, message):
        issues.append({'code': code, 'path': str(path), 'message': message})

    def read(path):
        path = Path(path)
        if path not in texts:
            try:
                texts[path] = path.read_text(encoding='utf-8-sig')
            except (OSError, UnicodeError) as error:
                issue('READ', path, str(error))
                texts[path] = ''
        return texts[path]

    def equal(label, value, expected, path):
        counts['identity_fields'] += 1
        if value != expected:
            issue('IDENTITY', path, f'{label}: {value!r}，应为 {expected!r}')

    def json_data(path):
        try:
            return json.loads(read(path))
        except json.JSONDecodeError as error:
            issue('JSON', path, str(error))
            return {}

    skills = sorted((root / 'skills').glob('*/SKILL.md'))
    if len(skills) != 1:
        issue('SKILL_SOURCE', root / 'skills', '本项目应有一个 Skill 维护源')
        return {'status': 'failed', 'checks': counts, 'issues': issues}
    skill_md = skills[0]
    skill, name = skill_md.parent, skill_md.parent.name
    equal('frontmatter name', _field(read(skill_md), 'name'), name, skill_md)
    ui = skill / 'agents/openai.yaml'
    equal('display_name', _field(read(ui), 'display_name'), name, ui)
    counts['identity_fields'] += 1
    if not re.search(rf'\${re.escape(name)}(?![\w-])', _field(read(ui), 'default_prompt') or ''):
        issue('IDENTITY', ui, 'default_prompt 未调用当前 Skill 名称')
    evals = skill / 'evals/evals.json'
    equal('eval skill_name', json_data(evals).get('skill_name'), name, evals)
    installer = skill / 'scripts/install.ps1'
    match = re.search(r'\.agents[\\/]skills[\\/]([a-z0-9-]+)', read(installer))
    equal('install destination', match.group(1) if match else None, name, installer)
    builder = root / 'tools/build_skill_zip.py'
    try:
        tree = ast.parse(read(builder))
        values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'SKILL_NAME' for t in node.targets)
                  and isinstance(node.value, ast.Constant)]
        equal('builder SKILL_NAME', values, [name], builder)
    except SyntaxError as error:
        issue('PYTHON', builder, str(error))

    # 只检查当前入口与当前 Skill；带日期的复盘、旧回执及备份保留历史原文。
    docs = [root / n for n in ('AGENTS.md', 'CONTEXT.md', 'SOURCE_OF_TRUTH.md')]
    docs += [skill_md, *sorted((skill / 'references').glob('*.md'))]
    for doc in docs:
        text = read(doc)
        for match in re.finditer(r'\]\((<[^>]+>|[^\s)]+)\)', text):
            target = match.group(1).strip('<>')
            if urlsplit(target).scheme:
                continue
            path, _, anchor = unquote(target).partition('#')
            resolved = (doc.parent / path).resolve() if path else doc
            counts['document_links'] += 1
            if not resolved.exists():
                issue('DOC_LINK', doc, f'引用不存在：{target}')
            elif anchor and resolved.suffix == '.md' and anchor not in _headings(read(resolved)):
                issue('DOC_ANCHOR', doc, f'标题锚点不存在：{target}')
        pointers = re.findall(r'`(skills/[^`]+)`', text) if doc.name in ('AGENTS.md', 'SOURCE_OF_TRUTH.md') else []
        if doc.name == 'SOURCE_OF_TRUTH.md':
            for prefix in re.findall(r'^- (.+?)[：:]', text, re.MULTILINE):
                pointers += re.findall(r'`((?:\.scratch|scripts|tools|references)/[^`]+)`', prefix)
        for path in sorted(set(pointers)):
            if any(char in path for char in '*<>'):
                continue
            counts['source_pointers'] += 1
            if not (root / path).exists():
                issue('SOURCE_POINTER', doc, f'当前真源指针不存在：{path}')

    if user_dir is not None:
        user_dir = Path(user_dir)
        shared = user_dir / '.agents/skills' / name
        equal('shared entry target', shared.resolve(), skill.resolve(), shared)
        claude = user_dir / '.claude/skills' / name
        if claude.exists():
            equal('Claude entry target', claude.resolve(), skill.resolve(), claude)
        inventory = user_dir / '.agents/skill-inventory.json'
        entries = [s for s in json_data(inventory).get('skills', []) if s.get('name') == name]
        if len(entries) != 1:
            issue('INSTALL', inventory, '当前 Skill 库存记录缺失或重复')
        else:
            entry = entries[0]
            for field, expected in (('entry', shared), ('source', skill)):
                equal(f'inventory {field}', Path(entry.get(field, '')).resolve(), expected.resolve(), inventory)
            equal('inventory sha256', entry.get('sha256'), hashlib.sha256(skill_md.read_bytes()).hexdigest(), inventory)
        policy = user_dir / '.agents/skill-activation.json'
        if name in json_data(policy).get('retired', []):
            issue('INSTALL', policy, '当前 Skill 名称仍被列入退役名单')
    return {'status': 'failed' if issues else 'passed', 'checks': counts, 'issues': issues}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--installed', action='store_true', help='另检查本机联接、库存哈希和名称策略')
    args = parser.parse_args(argv)
    result = check(args.root, Path(os.environ.get('USERPROFILE', str(Path.home()))) if args.installed else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
