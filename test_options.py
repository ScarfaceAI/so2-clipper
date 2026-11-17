"""配置拆分后的运行身份、来源隔离和失败回执行为。"""
import copy
import contextlib
import io
import json
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import autolayout
import config
import kill_clipper
import media
import workspace
from options import Options, RunContext, ResolvedSource, copy_options


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='so2_context_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'source.mp4'
        self.video.write_bytes(b'identity fixture; media probe is explicitly supplied')
        self.args = ['--video', str(self.video), '--profile', 'pc', '--identity-mode', 'auto',
                     '--output-dir', str(self.root / 'output'), '--state-dir', str(self.root / 'state')]
        self.options = config.load_config(self.args)
        self.info = {'duration': 8., 'fps': 60., 'width': 1920, 'height': 1080,
                     'rotation': 0, 'has_audio': False}

    def test_resolving_source_keeps_options_and_owning_run_separate(self):
        before = copy.deepcopy(vars(self.options))
        context = RunContext.from_options(self.options, current_run_id='context-test', run_date='1999-12-31')
        with patch.object(media, 'probe', return_value=self.info):
            source = autolayout.resolve_source(context, str(self.video))
        self.assertIsInstance(self.options, Options)
        self.assertIsInstance(source, ResolvedSource)
        self.assertEqual(vars(self.options), before)
        self.assertEqual((source.current_run_id, source.run_date), ('context-test', '1999-12-31'))
        self.assertEqual(source.effective_engine, 'ffmpeg')
        self.assertEqual(source.media_info, self.info)
        self.assertFalse(hasattr(self.options, 'media_info'))

    def test_source_worker_copy_is_pickleable_and_keeps_run_date(self):
        context = RunContext.from_options(self.options, current_run_id='worker-test', run_date='1999-12-31')
        source = ResolvedSource.from_options(context, path=str(self.video), media_info=self.info,
                                              profile='pc', effective_engine='ffmpeg')
        worker = pickle.loads(pickle.dumps(copy_options(source, scan_segments=1)))
        self.assertEqual(worker.scan_segments, 1)
        self.assertEqual(source.scan_segments, self.options.scan_segments)
        self.assertEqual((worker.current_run_id, worker.run_date), ('worker-test', '1999-12-31'))
        self.assertEqual(worker.path, str(self.video))

    def test_failure_receipt_uses_the_context_created_inside_writer(self):
        with patch.object(kill_clipper, 'run', side_effect=ValueError('injected run failure')) as run:
            with contextlib.redirect_stderr(io.StringIO()):
                code = kill_clipper.main(self.args + ['--run-id', 'failed-context'])
        self.assertEqual(code, 1)
        self.assertIsInstance(run.call_args.args[0], RunContext)
        receipt = self.root / 'state/runs/failed-context/receipt.json'
        data = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(data['run_id'], 'failed-context')
        self.assertEqual(data['exit_code'], 1)
        self.assertIn('injected run failure', data['error'])

    def test_losing_writer_does_not_create_a_run_or_failure_receipt(self):
        with workspace.writer(self.options.state_dir):
            with patch.object(kill_clipper, 'run', side_effect=AssertionError('must not run')):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = kill_clipper.main(self.args + ['--run-id', 'blocked-context'])
        self.assertEqual(code, 1)
        self.assertFalse((self.root / 'state/runs/blocked-context').exists())


if __name__ == '__main__':
    unittest.main()
