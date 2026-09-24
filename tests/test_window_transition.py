"""Cross-provider cutover behavior; fake provider evidence, real isolated tmux."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import window_transition as wt


class HandoffTests(unittest.TestCase):
    def test_resume_rejects_live_rollout_when_process_session_id_is_missing(self):
        sid = '01900000-0000-7000-8000-000000000001'
        client = mock.Mock()
        client.snapshot.return_value = ([{'kind': 'Codex', 'ai_alive': True, 'pane_id': '%5'}], {})
        client.request.return_value = {'history': {'quality': 'full', 'transcript_id': 'rollout-date-' + sid}}
        with self.assertRaisesRegex(wt.TransitionError, 'already live'):
            wt.reject_live_resume(client, sid)
        client.request.return_value = {'history': {'quality': 'partial'}}
        with self.assertRaisesRegex(wt.TransitionError, 'full conversation'):
            wt.reject_live_resume(client, sid)

    def test_bounded_excerpts_exclude_tool_dumps(self):
        blocks = [{'role':'assistant','text':'A'*50000} for _ in range(20)]
        blocks += [{'role':'tool','text':'SECRET_TOOL_DUMP'}, {'role':'user','text':'latest question'}]
        text=wt.conversation_tail({'blocks':blocks})
        self.assertLessEqual(len(text),wt.MAX_HANDOFF_CHARS)
        self.assertIn('latest question',text)
        self.assertNotIn('SECRET_TOOL_DUMP',text)
        self.assertIn('excerpt omitted',text)

    def test_resumption_validates_provider_id_and_workspace_before_copy(self):
        sid='01900000-0000-7000-8000-000000000001'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); src=root/'old'; dst=root/'new'
            p=src/'sessions/2026/09/09'/f'rollout-date-{sid}.jsonl'
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'cwd':'/project'}})+'\n')
            (src/'session_index.jsonl').write_text(json.dumps({'id':'unrelated'})+'\n'+json.dumps({'id':sid})+'\n')
            with self.assertRaises(wt.TransitionError):
                wt.copy_codex_session(src,dst,sid,'/wrong-project')
            self.assertFalse(dst.exists())
            wt.copy_codex_session(src,dst,sid,'/project')
            self.assertEqual(p.read_bytes(),(dst/p.relative_to(src)).read_bytes())
            self.assertNotIn('unrelated',(dst/'session_index.jsonl').read_text())
            with self.assertRaises(wt.TransitionError):
                wt.copy_codex_session(src,dst,sid,'/project')

    def test_launch_argv_preserves_literal_paths_and_never_uses_resume_last(self):
        plan=dict(handoff='/project/a \' $(touch NO)/handoff.md',handoff_sha256='abcd',to='codex',model='small',resume_id='exact')
        args=wt.build_launch(plan,'/project/codex home')
        self.assertIn('CODEX_HOME=/project/codex home',args)
        self.assertEqual(args[4:6],['resume','exact'])
        self.assertNotIn('--last',args)
        self.assertIn('$(touch NO)',args[-1])  # remains inert prompt data


class FakeCards:
    def __init__(self, case):
        self.case=case; self.posts=[]; self.ready=False; self.echo_only=False; self.status='idle'

    def snapshot(self):
        c=self.case
        result=[dict(c.source, kind='Claude',ai_alive=True,window_name='original',identity_fidelity='exact')]
        if c.plan.get('target'):
            live=wt.pane_location(c.plan['target']['pane_id'])
            result.append(dict(live,kind='Codex',ai_alive=True))
        return result,{}

    def request(self,method,path,payload=None):
        if method=='POST':
            self.posts.append(payload); return {'prefs':{}}
        from urllib.parse import parse_qs,urlparse
        pane=parse_qs(urlparse(path).query)['pane'][0]
        if pane==self.case.source['pane_id']:
            return {'status':self.status,'history':{'quality':'full','transcript_id':'claude-source'},
                    'blocks':[{'role':'user','text':'Please improve the module.'}]}
        marker='HANDOFF_READY_'+self.case.plan['handoff_sha256']
        blocks=[{'role':'user','text':marker}]
        if self.ready:
            blocks += [{'role':'assistant','text':'Read the task. '+marker,'final':True}]
            if not self.echo_only:
                blocks += [{'role':'tool','label':'Tool result','text':self.case.plan['handoff_sha256']+' handoff.md'}]
        return {'status':'idle','history':{'quality':'full','transcript_id':'codex-target'},'blocks':blocks}


@unittest.skipUnless(shutil.which('tmux'), 'tmux required')
class TransitionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.socket=str(self.base/'tmux.sock')
        def run_tmux(*args):
            result=subprocess.run(['tmux','-S',self.socket,*args],capture_output=True,text=True,timeout=10,check=True)
            return result.stdout.strip()
        self.tmux=run_tmux
        self.tmux('new-session','-d','-s','test','-n','original','-c',str(self.base),'/bin/sleep','120')
        self.patches=[mock.patch.object(wt,'tmux',side_effect=run_tmux),
                      mock.patch.object(wt.recovery,'tmux_server_id',return_value='test-server:1'),
                      mock.patch.object(wt,'TRANSITIONS',self.base/'transitions')]
        for p in self.patches:p.start()
        pane=self.tmux('display-message','-p','-t','test:0','#{pane_id}')
        self.source=wt.pane_location(pane)
        self.plan={}; self.client=FakeCards(self)
        self.plan=wt.prepare(SimpleNamespace(selector=pane,to='codex',model='small',resume_id='',codex_home=''),self.client)
        self.path=Path(self.plan['plan'])

    def tearDown(self):
        self.tmux('kill-server')
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def launch(self):
        # The tmux/window lifecycle is real; model execution is independently tested.
        with mock.patch.object(wt.recovery,'run',return_value=SimpleNamespace(stdout=str(self.base/'codex-home'))), \
             mock.patch.object(wt,'build_launch',return_value=['/bin/sleep','120']):
            wt.launch(self.plan,self.path,self.client)

    def test_launch_pins_provider_and_codex_home_on_the_new_pane(self):
        # Once the AI exits, restart/recovery only have these pane options to
        # know which isolated CODEX_HOME holds the conversation.
        self.launch()
        pane=self.plan['target']['pane_id']
        self.assertEqual(self.tmux('show-option','-p','-v','-t',pane,'@ai_provider'),'codex')
        self.assertEqual(self.tmux('show-option','-p','-v','-t',pane,'@ai_codex_home'),str(self.base/'codex-home'))

    def test_success_preserves_source_and_original_number_without_duplicate_launch(self):
        self.assertTrue(self.source['pane_start_time'].startswith(self.source['pane_pid'] + ':'))
        self.launch()
        self.assertEqual(
            self.tmux('show-option', '-p', '-v', '-t', self.plan['target']['pane_id'], '@ai_cwd'),
            self.source['cwd'],
        )
        before=self.tmux('list-windows','-t','test','-F','#{window_id}')
        wt.launch(self.plan,self.path,self.client)
        self.assertEqual(before,self.tmux('list-windows','-t','test','-F','#{window_id}'))
        self.client.ready=True
        wt.promote(self.plan,self.path,self.client)
        self.assertEqual(self.plan['state'],'promoted')
        self.assertEqual(wt.pane_location(self.plan['target']['pane_id'])['window_index'],self.source['window_index'])
        self.assertTrue(wt.same_identity(self.source,wt.pane_location(self.source['pane_id'])))
        self.assertNotEqual(self.plan['backup']['window_index'],self.source['window_index'])
        self.assertEqual(len(self.client.posts),3)

    def test_missing_ack_or_only_echo_never_moves_source(self):
        self.launch()
        with self.assertRaises(wt.TransitionError):wt.promote(self.plan,self.path,self.client)
        self.client.ready=True; self.client.echo_only=True
        with self.assertRaises(wt.TransitionError):wt.promote(self.plan,self.path,self.client)
        self.assertEqual(wt.pane_location(self.source['pane_id'])['window_index'],self.source['window_index'])

    def test_changed_source_does_not_launch_or_destroy_any_window(self):
        self.client.status='running'
        with self.assertRaises(wt.TransitionError):self.launch()
        self.assertEqual(len(self.tmux('list-windows','-t','test','-F','#{window_id}').splitlines()),1)

    def test_changed_handoff_rejected_before_launch(self):
        Path(self.plan['handoff']).write_text('changed')
        with self.assertRaises(wt.TransitionError):self.launch()
        self.assertEqual(len(self.tmux('list-windows','-t','test','-F','#{window_id}').splitlines()),1)

    def test_short_brief_preserves_original_source_change_guard(self):
        brief = self.base / 'brief.md'
        brief.write_text('Brief context only; verify live project state before work.')
        plan = wt.prepare(SimpleNamespace(selector=self.source['pane_id'], to='codex', model='',
                          resume_id='', codex_home='', context_file=brief), self.client)
        text = Path(plan['handoff']).read_text()
        self.assertIn(brief.read_text(), text)
        self.assertNotIn('Please improve the module.', text)
        self.assertEqual(plan['excerpt_sha256'], self.plan['excerpt_sha256'])
        wt.assert_source(plan, self.client)

    def test_metadata_failure_preserves_both_panes_and_number(self):
        self.launch(); self.client.ready=True
        real=self.client.request
        def fail(method,*args,**kwargs):
            if method=='POST':raise wt.TransitionError('metadata failed')
            return real(method,*args,**kwargs)
        with mock.patch.object(self.client,'request',side_effect=fail),self.assertRaises(wt.TransitionError):
            wt.promote(self.plan,self.path,self.client)
        self.assertEqual(wt.pane_location(self.source['pane_id'])['window_index'],self.source['window_index'])
        self.assertEqual(len(self.tmux('list-windows','-t','test','-F','#{window_id}').splitlines()),2)

    def test_quota_source_can_prepare_but_not_promote_scheduled_work(self):
        self.client.status='quota_limited'
        self.launch(); self.client.ready=True
        with self.assertRaisesRegex(wt.TransitionError,'source must be idle'):
            wt.promote(self.plan,self.path,self.client)
        self.assertEqual(wt.pane_location(self.source['pane_id'])['window_index'],0)

    def test_pending_user_input_blocks_even_when_excerpt_hash_unchanged(self):
        real=self.client.request
        def queued(method,path,payload=None):
            result=real(method,path,payload)
            if method=='GET':
                result['blocks'].append({'role':'user','text':'new instruction','pending':True})
            return result
        with mock.patch.object(self.client,'request',side_effect=queued),self.assertRaisesRegex(wt.TransitionError,'queued input'):
            self.launch()

    def test_retries_after_swap_do_not_swap_back_or_lose_backup_alias(self):
        self.launch(); self.client.ready=True
        real=self.client.request; calls=0
        def fail_second(method,*args,**kwargs):
            nonlocal calls
            if method=='POST':
                calls+=1
                if calls==2:raise wt.TransitionError('temporary metadata failure after swap')
            return real(method,*args,**kwargs)
        with mock.patch.object(self.client,'request',side_effect=fail_second),self.assertRaises(wt.TransitionError):
            wt.promote(self.plan,self.path,self.client)
        self.assertEqual(self.plan['state'],'promoting')
        target_id=self.plan['target']['pane_id']
        self.assertEqual(wt.pane_location(target_id)['window_index'],0)
        wt.promote(self.plan,self.path,self.client)
        self.assertEqual(wt.pane_location(target_id)['window_index'],0)
        self.assertTrue(self.client.posts[-1]['alias'].startswith('切换前备份-'))
        self.assertFalse(self.client.posts[-1]['favorite'])

    def test_cards_and_tmux_pid_mismatch_blocks_preparation(self):
        panes,prefs=self.client.snapshot(); panes[0]['pane_pid']='123'
        with mock.patch.object(self.client,'snapshot',return_value=(panes,prefs)), self.assertRaisesRegex(wt.TransitionError,'different pane/server'):
            wt.prepare(SimpleNamespace(selector=self.source['pane_id'],to='codex',model='',resume_id='',codex_home=''),self.client)

class IsolatedHomeTests(unittest.TestCase):
    def test_rerun_does_not_duplicate_trust_tables_or_create_nested_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source'; homes=root/'homes'; project=root/'quoted "project"'
            project.mkdir(); (source/'scripts').mkdir(parents=True)
            (source/'rules').mkdir(); (source/'skills').mkdir(); (source/'auth.json').write_text('{}')
            installer=source/'scripts/installer.sh'
            installer.write_text('#!/bin/sh\ntouch "$CODEX_HOME/config.toml"\n')
            installer.chmod(0o700)
            env=dict(os.environ,AGENT_BUS_DEFAULT_CODEX_HOME=str(source),AGENT_BUS_CODEX_HOMES_ROOT=str(homes))
            command=['bash',str(Path(wt.__file__).with_name('create-isolated-codex-home.sh')),'sample',str(project)]
            subprocess.run(command,env=env,capture_output=True,text=True,check=True)
            config=homes/'sample/config.toml'
            with config.open('a') as stream:stream.write('custom_key = "preserved"\n')
            subprocess.run(command,env=env,capture_output=True,text=True,check=True)
            text=config.read_text()
            self.assertEqual(text.count('[projects.'),1)
            self.assertEqual(text.count('trust_level ='),1)
            self.assertIn('custom_key = "preserved"',text)
            self.assertIn('\\"project\\"',text)
            self.assertFalse((source/'rules/rules').exists())
            self.assertFalse((source/'skills/skills').exists())
