"""Automation model contract for `codex start` workers."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import codex_app


class FakeApp:
    def __init__(self, reject=False):
        self.requests = []
        self.reject = reject
        self.stderr_lines = []

    def request(self, method, params):
        self.requests.append((method, params))
        if method == 'thread/start':
            if self.reject:
                raise SystemExit('model is not supported')
            return {'thread': {'id': 'cost-thread'}}
        if method == 'thread/name/set':
            return {}
        if method == 'turn/start':
            return {'turn': {'id': 'cost-turn'}}
        raise AssertionError(method)

    def stream_until_complete(self, thread_id, turn_id, run_dir, **_kwargs):
        return {'status': 'completed', 'diagnosis': 'completed', 'reply': 'OK', 'diff': '',
                'status_file': str(run_dir / 'status.json'), 'warnings': []}


class AgentModelPolicyTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(codex_app, 'RUNS', root / 'runs'))
        self.stack.enter_context(patch.object(codex_app, 'RUN_INDEX', root / 'index.json'))
        self.stack.enter_context(patch.object(codex_app, 'ensure_worker_home', return_value=str(root / 'home')))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def assert_policy(self, app, model='gpt-5.6-luna', effort='low'):
        start = next(params for method, params in app.requests if method == 'thread/start')
        turn = next(params for method, params in app.requests if method == 'turn/start')
        self.assertEqual(start['model'], model)
        self.assertEqual(start['config']['model_reasoning_effort'], effort)
        self.assertEqual(turn['effort'], effort)

    def start(self, app, **overrides):
        values = dict(task='Reply OK', task_file='', model='', name='', repo='', approval='never',
                      sandbox='read-only', timeout=1, wait=True, wait_timeout=5, idle_timeout=0,
                      progress_interval=0, fail_on_idle=False, fail_on_error=False, shared_home=False,
                      no_mcp=False, codex_config=['model="gpt-6-astra"', 'model_reasoning_effort="high"'])
        values.update(overrides)
        with patch.object(codex_app, 'AppServer') as factory:
            factory.return_value.__enter__.return_value = app
            codex_app.cmd_start(Namespace(**values))

    def test_direct_start_uses_low_cost_even_with_expensive_interactive_config(self):
        app = FakeApp()
        self.start(app)
        self.assert_policy(app)
        run = next(iter(json.loads(codex_app.RUN_INDEX.read_text())['runs'].values()))
        self.assertEqual(run['model'], 'gpt-5.6-luna')
        self.assertEqual(run['reasoning_effort'], 'low')

    def test_explicit_model_and_effort_override_defaults(self):
        app = FakeApp()
        self.start(app, model='gpt-5.6-terra', reasoning_effort='medium')
        self.assert_policy(app, 'gpt-5.6-terra', 'medium')

    def test_model_rejection_does_not_fallback_to_expensive_model(self):
        app = FakeApp(reject=True)
        with self.assertRaisesRegex(SystemExit, 'model is not supported'):
            self.start(app)
        self.assertEqual([method for method, _ in app.requests], ['thread/start'])
        self.assertEqual(app.requests[0][1]['model'], 'gpt-5.6-luna')
        run = next(iter(json.loads(codex_app.RUN_INDEX.read_text())['runs'].values()))
        self.assertEqual(run['status'], 'start_failed')
        self.assertEqual(run['diagnosis'], 'model_rejected')


if __name__ == '__main__':
    unittest.main()
