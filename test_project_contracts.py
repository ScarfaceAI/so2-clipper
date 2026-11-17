"""名称迁移与文档漂移检查的故障注入；不使用用户配置或录像。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'tools/check_project_contracts.py'


class ProjectContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='so2_contract_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write('skills/so2-clipper/SKILL.md', '---\nname: so2-clipper\ndescription: clips\n---\n[工作流](references/workflow.md#实际验收)\n')
        self.write('skills/so2-clipper/agents/openai.yaml', 'interface:\n  display_name: "so2-clipper"\n  default_prompt: "使用 $so2-clipper 剪辑"\n')
        self.write('skills/so2-clipper/evals/evals.json', '{"skill_name": "so2-clipper"}')
        self.write('skills/so2-clipper/scripts/install.ps1', "param($Destination = '.agents\\skills\\so2-clipper')\n")
        self.write('skills/so2-clipper/references/workflow.md', '# 实际验收\n')
        self.write('tools/build_skill_zip.py', "SKILL_NAME = 'so2-clipper'\n")
        self.write('AGENTS.md', '`skills/so2-clipper/`\n')
        self.write('CONTEXT.md', '# 术语\n')
        self.write('SOURCE_OF_TRUTH.md', '- `.scratch/original/spec.md`：历史计划。\n')
        self.write('.scratch/original/spec.md', '# 原始计划\n')

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def run_check(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root)],
                                capture_output=True, encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'})
        return result.returncode, json.loads(result.stdout)

    def test_consistent_source_passes_without_building_or_runtime(self):
        code, report = self.run_check()
        self.assertEqual(code, 0, report)
        self.assertEqual(report['issues'], [])
        self.assertFalse((self.root / 'dist').exists())

    def test_directory_only_rename_is_rejected(self):
        (self.root / 'skills/so2-clipper').rename(self.root / 'skills/new-clipper')
        code, report = self.run_check()
        self.assertEqual(code, 1)
        self.assertTrue(any(i['code'] == 'IDENTITY' for i in report['issues']))

    def test_drifted_installer_and_builder_are_both_reported(self):
        self.write('skills/so2-clipper/scripts/install.ps1', "param($Destination = '.agents\\skills\\old-clipper')\n")
        self.write('tools/build_skill_zip.py', "SKILL_NAME = 'old-clipper'\n")
        code, report = self.run_check()
        self.assertEqual(code, 1)
        paths = {Path(i['path']).name for i in report['issues'] if i['code'] == 'IDENTITY'}
        self.assertTrue({'install.ps1', 'build_skill_zip.py'} <= paths)

    def test_rewritten_historical_pointer_fails_and_original_path_passes(self):
        before = (self.root / '.scratch/original/spec.md').read_bytes()
        self.write('SOURCE_OF_TRUTH.md', '- `.scratch/new-clipper/spec.md`：历史计划。\n')
        code, report = self.run_check()
        self.assertEqual(code, 1)
        self.assertTrue(any(i['code'] == 'SOURCE_POINTER' for i in report['issues']))
        self.write('SOURCE_OF_TRUTH.md', '- `.scratch/original/spec.md`：历史计划。\n')
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual((self.root / '.scratch/original/spec.md').read_bytes(), before)

    def test_missing_workflow_or_heading_fails(self):
        self.write('skills/so2-clipper/SKILL.md', '---\nname: so2-clipper\n---\n[流程](references/missing.md)\n[验收](references/workflow.md#旧标题)\n')
        code, report = self.run_check()
        self.assertEqual(code, 1)
        self.assertTrue({'DOC_LINK', 'DOC_ANCHOR'} <= {i['code'] for i in report['issues']})

    def test_every_source_in_a_shared_index_line_is_checked(self):
        self.write('scripts/delivery.py', '# existing tool\n')
        self.write('SOURCE_OF_TRUTH.md', '- `scripts/delivery.py`、`scripts/missing_evidence.py`：当前交付工具。\n')
        code, report = self.run_check()
        self.assertEqual(code, 1)
        self.assertTrue(any(i['code'] == 'SOURCE_POINTER' and 'scripts/missing_evidence.py' in i['message']
                            for i in report['issues']))
        self.write('scripts/missing_evidence.py', '# restored tool\n')
        self.assertEqual(self.run_check()[0], 0)
