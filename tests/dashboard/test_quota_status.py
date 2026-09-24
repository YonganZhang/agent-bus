"""Quota pauses are runtime notices, never assistant replies or tasks."""
import unittest
from unittest import mock
import server
from test_pane_status import claude_screen

QUOTA = '  ⚠ Usage limit reached · continuing automatically at 10pm · esc to cancel'
SCREEN = claude_screen(
    '❯ latest request',
    "  ⎿ You've hit your session limit · resets 10pm (UTC)",
    '     Continuing automatically at 10pm · esc to cancel',
    '     /usage-credits to continue now',
    '● Usage limit reached · continuing automatically at 10pm · esc or type to cancel',
    '✻ Churned for 0s · done 7:36 pm',
    ' ' * 80 + '✔ Update installed · Restart to update',
    chrome=QUOTA + '\n  ⚠ /usage-credits to continue now\n  ⏵⏵ auto mode on (shift+tab to cycle)',
)

class QuotaStatusTest(unittest.TestCase):
    def test_live_quota_overrides_transcript_and_activity(self):
        self.assertEqual(server.infer_status(SCREEN, 'claude'), 'quota_limited')
        for recorded in ('running', 'idle'):
            with self.subTest(recorded=recorded), mock.patch.object(server, 'transcript_activity_status', return_value=recorded):
                self.assertEqual(server.infer_pane_status('%test', SCREEN, 'claude', 'Claude', True, 'test.jsonl'), 'quota_limited')

    def test_quoted_or_old_quota_does_not_pause_a_recovered_pane(self):
        screen = claude_screen('❯ Explain this warning', QUOTA, '● It describes an earlier limit.')
        self.assertEqual(server.infer_status(screen, 'claude'), 'idle')

    def test_terminal_notices_do_not_become_conversation_or_todos(self):
        blocks = server.parse_blocks(SCREEN, 'Claude')
        self.assertEqual([(b['role'], b['text']) for b in blocks], [('user', 'latest request')])
        self.assertNotIn('Usage limit', server.preview_from(SCREEN))
        self.assertNotIn('Update installed', server.preview_from(SCREEN))

    def test_real_todo_and_prose_survive(self):
        blocks = server.parse_blocks(claude_screen('● We will review the usage limit tomorrow.', '✔ Verify results'), 'Claude')
        text = '\n'.join(b['text'] for b in blocks)
        self.assertIn('review the usage limit', text)
        self.assertIn('Verify results', text)

class QuotaFrontendTest(unittest.TestCase):
    def test_quota_overrides_old_reply_and_optimistic_running_then_recovers(self):
        from test_frontend_realtime import function_source, INDEX
        import subprocess
        source = INDEX.read_text()
        functions = '\n'.join(function_source(source, name, next_name) for name, next_name in [
            ('jobStatusLabel','effectiveJobStatus'), ('effectiveJobStatus','jobPill'),
            ('jobPill','jobMetaValue'), ('paneIsRunning','paneIsProcessing'),
            ('paneAwaitsReply','syncPaneThinkingSince'),
        ])
        script = '''
const THINKING_JOB_STATUSES = new Set(['sent','running']);
const escapeHtml = String;
const isOptimisticRunning = () => true;
const pendingLocalPrompts = () => ['pending'];
''' + functions + '''
const pane = {pane_id:'%test',status:'quota_limited',runtime_status:'running',job:{status:'running',response_started_at:'yesterday'}};
if(paneIsRunning(pane) || paneAwaitsReply(pane)) throw Error('quota must suppress processing');
const html=jobPill(pane.job,pane);
if(!html.includes('额度已满，静待恢复') || html.includes('已有回复')) throw Error(html);
pane.status='running';
if(!paneIsRunning(pane) || effectiveJobStatus(pane.job,pane)!=='running') throw Error('recovery stuck');
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
