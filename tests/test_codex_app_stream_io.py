"""Exercise actual OS pipes; mocked event iterators cannot catch buffering stalls."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import codex_app


class AppServerPipeTest(unittest.TestCase):
    def start_burst_server(self, messages):
        payload = '\n'.join(json.dumps(message) for message in messages) + '\n'
        script = 'import os,sys\nsys.stdin.readline()\nos.write(1, ' + repr(payload.encode()) + ')\nsys.stdin.read()\n'
        return self.start_script_server(script)

    def start_script_server(self, script):
        proc = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.addCleanup(self.close_process, proc)
        with patch.object(codex_app.subprocess, 'Popen', return_value=proc):
            return codex_app.AppServer(timeout=0.2)

    @staticmethod
    def close_process(proc):
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()

    def test_reply_and_terminal_event_in_one_pipe_write_complete_without_timeout(self):
        app = self.start_burst_server([
            {'id':1, 'result':{}},
            {'method':'item/agentMessage/delta', 'params':{'threadId':'t', 'turnId':'u', 'delta':'OK'}},
            {'method':'turn/completed', 'params':{'threadId':'t', 'turn':{'id':'u', 'status':'completed'}}},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            result = app.stream_until_complete('t', 'u', Path(tmp), timeout=0.3, progress_interval=0)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['reply'], 'OK')

    def test_partial_stderr_and_utf8_json_obey_timeout_then_resume(self):
        message = {'method': '测试', 'params': {}}
        wire = (json.dumps(message, ensure_ascii=False) + '\n').encode()
        split_at = wire.index('测'.encode()) + 1
        script = ('import os,sys\nsys.stdin.readline()\n'
                  'os.write(1, ' + repr(b'{"id":1,"result":{}}\n') + ')\n'
                  'sys.stdin.readline()\n'
                  'os.write(2, b"partial diagnostic")\n'
                  'os.write(1, ' + repr(wire[:split_at]) + ')\n'
                  'sys.stdin.readline()\n'
                  'os.write(2, b" completed\\n")\n'
                  'os.write(1, ' + repr(wire[split_at:]) + ')\n'
                  'sys.stdin.read()\n')
        app = self.start_script_server(script)
        started = time.monotonic()
        self.assertIsNone(app._read_line(timeout=0.1))
        self.assertLess(time.monotonic() - started, 0.5)
        app.notify('finish', {})
        self.assertEqual(app._read_line(timeout=1), message)
        # A second read drains diagnostics even if stdout was returned first.
        self.assertIsNone(app._read_line(timeout=0.05))
        self.assertEqual(app.stderr_lines, ['partial diagnostic completed'])

    def test_terminal_notification_during_idle_status_rpc_is_not_discarded(self):
        initial = [
            {'id': 1, 'result': {}},
            {'method':'item/agentMessage/delta', 'params':{'threadId':'t', 'turnId':'u', 'delta':'OK'}},
            {'method':'thread/status/changed', 'params':{'threadId':'t', 'status':{'type':'idle'}}},
        ]
        terminal = {'method':'turn/completed', 'params':{'threadId':'t', 'turn':{'id':'u','status':'completed'}}}
        initial_wire = ('\n'.join(json.dumps(x) for x in initial) + '\n').encode()
        script = ('import os,sys,json\nsys.stdin.readline()\n'
                  'os.write(1, ' + repr(initial_wire) + ')\n'
                  'sys.stdin.readline()\n'
                  'request=json.loads(sys.stdin.readline())\n'
                  'reply={"id":request["id"],"result":{"thread":{"id":"t","turns":[]}}}\n'
                  'os.write(1, (' + repr(json.dumps(terminal) + '\n') + '+json.dumps(reply)+"\\n").encode())\n'
                  'sys.stdin.read()\n')
        app = self.start_script_server(script)
        with tempfile.TemporaryDirectory() as tmp:
            result = app.stream_until_complete('t', 'u', Path(tmp), timeout=0.3, progress_interval=0)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['reply'], 'OK')

    def test_initialize_failure_closes_server_and_keeps_stderr(self):
        # Finding 4: stderr from an app-server that dies during initialize must
        # reach the caller so cmd_start can write it next to the run.
        script = 'import os,sys\nos.write(2, b"Error: invalid config.toml\\n")\nsys.exit(3)\n'
        proc = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.addCleanup(self.close_process, proc)
        with patch.object(codex_app.subprocess, 'Popen', return_value=proc):
            with self.assertRaises(codex_app.AppServerStartError) as ctx:
                codex_app.AppServer(timeout=2)
        self.assertIn('Error: invalid config.toml', ctx.exception.stderr_lines)
        self.assertIsNotNone(proc.poll())

    def test_popen_receives_explicit_codex_home(self):
        # Finding 3: the worker CODEX_HOME is passed explicitly, not inherited.
        captured = {}
        real = subprocess.Popen

        def fake_popen(command, **kwargs):
            captured.update(kwargs)
            script = 'import os,sys\nsys.stdin.readline()\nos.write(1, b\'{"id":1,"result":{}}\\n\')\nsys.stdin.read()\n'
            proc = real([sys.executable, '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
            self.addCleanup(self.close_process, proc)
            return proc

        with patch.object(codex_app.subprocess, 'Popen', side_effect=fake_popen):
            app = codex_app.AppServer(timeout=2, codex_home='/homes/app-worker')
        self.addCleanup(app.close)
        self.assertEqual(captured['env']['CODEX_HOME'], '/homes/app-worker')
        self.assertEqual(app.codex_home, '/homes/app-worker')
