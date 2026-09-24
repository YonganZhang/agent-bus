#!/usr/bin/env python3
"""Regression tests for Cards pane-switch and optimistic reply state."""

from pathlib import Path
import subprocess
import unittest


INDEX = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html")


def function_source(source: str, name: str, next_name: str) -> str:
    marker = f"    function {name}("
    async_marker = f"    async function {name}("
    start = source.find(marker)
    if start < 0:
        start = source.index(async_marker)
    next_markers = [
        source.find(f"\n    function {next_name}(", start),
        source.find(f"\n    async function {next_name}(", start),
    ]
    end = min(index for index in next_markers if index >= 0)
    return source[start:end]


class FrontendRealtimeUxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INDEX.read_text(encoding="utf-8")

    def run_node(self, script: str) -> None:
        result = subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_switch_hides_cached_history_until_authoritative_capture(self) -> None:
        switch = function_source(self.source, "switchToPane", "updateSelectedHighlight")
        script = f"""
const calls = {{ loading: 0, cached: 0, capture: 0 }};
const state = {{
  panes: [{{ pane_id: '%2' }}], selected: '%1', sharedFilesOpen: false,
  rawVisible: false, traceOpen: false, blocksByPane: new Map([['%2', [{{text:'old'}}]]]),
  blocksCachedAtByPane: new Map([['%2', Date.now()]]),
  favoriteCapturePrefetching: new Map(), captureKickPending: false,
  lastPaneRefreshAt: 0, currentBlocks: [{{text:'old'}}]
}};
function clearTimelineTextSelection() {{}}
function saveDraft() {{}}
function setSharedFilesOpen() {{}}
function requestBottomScroll() {{}}
function loadDraft() {{}}
function selectTracePane() {{}}
function updateLastPromptButton() {{}}
function renderDetailPlanTools() {{}}
function isMobileLayout() {{ return false; }}
function setMobileDrawer() {{}}
function clearCaptureLoading() {{}}
function syncTerminalModeForPane() {{}}
function mergeOptimisticUserPrompts() {{ return []; }}
function freshFavoriteCapture() {{ return null; }}
function renderCachedPane() {{ calls.cached += 1; return true; }}
function startCaptureLoading() {{ calls.loading += 1; }}
function renderTimeline() {{}}
function updateSelectedHighlight() {{}}
function setCaptureLoadingProgress() {{}}
async function loadCapture() {{ calls.capture += 1; }}
function renderCards() {{}}
function scheduleFavoriteNeighborPrefetch() {{}}
function scheduleRefresh() {{}}
function failCaptureLoading() {{}}
function setTimelineCacheNotice() {{}}
function updateLiveStatus() {{}}
{switch}
(async () => {{
  await switchToPane('%2');
  if (calls.loading !== 1) throw new Error(`loading=${{calls.loading}}`);
  if (calls.cached !== 0) throw new Error(`cached=${{calls.cached}}`);
  if (calls.capture !== 1) throw new Error(`capture=${{calls.capture}}`);
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        self.run_node(script)

    def test_pending_prompt_survives_capture_gap_until_stably_confirmed(self) -> None:
        normalize = function_source(self.source, "normalizePromptForMatch", "addOptimisticUserPrompt")
        merge = function_source(self.source, "mergeOptimisticUserPrompts", "paneListSignature")
        script = f"""
const state = {{ optimisticUserPromptsByPane: new Map() }};
function blockScrollKey(block) {{ return `${{block.role}}:${{block.text}}`; }}
{normalize}
{merge}
const pane = '%2';
const anchor = {{role:'assistant', label:'AI output', text:'previous answer'}};
state.optimisticUserPromptsByPane.set(pane, [{{
  id: 'send-1', text: 'hello', at: Date.now(), delivery: 'delivered', afterKey: blockScrollKey(anchor)
}}]);
let merged = mergeOptimisticUserPrompts(pane, [
  anchor,
  {{role:'user', label:'User prompt', text:'hello'}},
  {{role:'assistant', label:'Todo', text:'- [ ] still working', pending:true}}
]);
if (!state.optimisticUserPromptsByPane.has(pane)) throw new Error('todo cleared pending prompt');
if (merged.some(block => block.optimistic)) throw new Error('confirmed prompt rendered twice');
merged = mergeOptimisticUserPrompts(pane, [
  anchor,
  {{role:'assistant', label:'Todo', text:'- [ ] still working', pending:true}}
]);
const fallbackIndex = merged.findIndex(block => block.optimistic);
const todoIndex = merged.findIndex(block => block.label === 'Todo');
if (fallbackIndex < 0) throw new Error('prompt vanished during capture gap');
if (fallbackIndex >= todoIndex) throw new Error('fallback prompt moved below live todo');
merged = mergeOptimisticUserPrompts(pane, [
  anchor,
  {{role:'user', label:'User prompt', text:'hello'}},
  {{role:'assistant', label:'AI output', text:'reply started'}}
], {{stableConfirmMs: 30000}});
if (!state.optimisticUserPromptsByPane.has(pane)) throw new Error('first reply frame cleared fallback too early');
const item = state.optimisticUserPromptsByPane.get(pane)[0];
item.confirmedAt = Date.now() - 31000;
mergeOptimisticUserPrompts(pane, [
  anchor,
  {{role:'user', label:'User prompt', text:'hello'}},
  {{role:'assistant', label:'AI output', text:'reply stable'}}
], {{stableConfirmMs: 30000}});
if (state.optimisticUserPromptsByPane.has(pane)) throw new Error('stable confirmed prompt was never cleaned');
"""
        self.run_node(script)

    def test_repeated_old_prompt_cannot_confirm_new_send_before_anchor(self) -> None:
        normalize = function_source(self.source, "normalizePromptForMatch", "addOptimisticUserPrompt")
        merge = function_source(self.source, "mergeOptimisticUserPrompts", "paneListSignature")
        script = f"""
const state = {{ optimisticUserPromptsByPane: new Map() }};
function blockScrollKey(block) {{ return `${{block.role}}:${{block.text}}`; }}
{normalize}
{merge}
const pane = '%98';
const anchor = {{role:'assistant', label:'AI output', text:'latest answer before send'}};
state.optimisticUserPromptsByPane.set(pane, [{{
  id:'send-repeat', text:'继续', at:Date.now(), delivery:'delivered', afterKey:blockScrollKey(anchor),
  priorCount: 1
}}]);
let merged = mergeOptimisticUserPrompts(pane, [
  {{role:'user', label:'User prompt', text:'继续'}},
  {{role:'assistant', label:'AI output', text:'old reply'}},
  anchor
], {{stableConfirmMs: 30000}});
if (!state.optimisticUserPromptsByPane.has(pane)) throw new Error('old duplicate cleared new send');
const optimisticIndex = merged.findIndex(block => block.optimistic);
if (optimisticIndex < 0) throw new Error('new repeated prompt not visible');
if (optimisticIndex !== merged.length - 1) throw new Error('new repeated prompt not anchored after latest answer');
merged = mergeOptimisticUserPrompts(pane, [
  {{role:'user', label:'User prompt', text:'继续'}},
  {{role:'assistant', label:'AI output', text:'old reply'}}
], {{stableConfirmMs: 30000}});
if (!merged.some(block => block.optimistic)) throw new Error('lost anchor let an old duplicate hide the new send');
"""
        self.run_node(script)

    def test_lost_anchor_still_confirms_a_first_time_prompt(self) -> None:
        """锚点丢失(块在流式增长或被 tail 窗口挤出)不该让气泡永远挂着。

        用户实测症状: 发一条命令, 卡片上并排显示两条一模一样的命令。这句话在发送时 transcript 里一次都没出现过
        (priorCount = 0), 所以 transcript 里冒出来的那条就是它自己的回声。
        """
        normalize = function_source(self.source, "normalizePromptForMatch", "addOptimisticUserPrompt")
        merge = function_source(self.source, "mergeOptimisticUserPrompts", "paneListSignature")
        script = f"""
const state = {{ optimisticUserPromptsByPane: new Map() }};
function blockScrollKey(block) {{ return `${{block.role}}:${{block.text}}`; }}
{normalize}
{merge}
const pane = '%99';
const anchor = {{role:'tool', label:'Tool', text:'still streaming...'}};
state.optimisticUserPromptsByPane.set(pane, [{{
  id:'send-fresh', text:'把卡片网站修好', at:Date.now(), delivery:'delivered',
  afterKey:blockScrollKey(anchor), priorCount: 0
}}]);
// 锚点块的文本已经变了 → 它的指纹再也找不回来(anchorIndex === -1)
const merged = mergeOptimisticUserPrompts(pane, [
  {{role:'tool', label:'Tool', text:'still streaming... done'}},
  {{role:'user', label:'User prompt', text:'把卡片网站修好'}}
], {{stableConfirmMs: 30000}});
const optimistic = merged.filter(block => block.optimistic);
if (optimistic.length) throw new Error('lost anchor left a duplicate optimistic bubble');
const prompts = merged.filter(block => block.role === 'user' && block.text === '把卡片网站修好');
if (prompts.length !== 1) throw new Error(`prompt rendered ${{prompts.length}} times, expected 1`);
"""
        self.run_node(script)

    def test_waiting_reply_stops_after_first_response_and_has_time_ceiling(self) -> None:
        parse_start = function_source(self.source, "parseThinkingStart", "jobThinkingStart")
        pending = function_source(self.source, "pendingLocalPrompts", "paneHasStartedResponse")
        response = function_source(self.source, "paneHasStartedResponse", "paneAwaitsReply")
        awaits = function_source(self.source, "paneAwaitsReply", "syncPaneThinkingSince")
        script = f"""
const THINKING_JOB_STATUSES = new Set(['created','sent','queued','leased','starting','running']);
const LEDGER_WAIT_MAX_MS = 15 * 60 * 1000;
const RESPONSE_ECHO_GRACE_MS = 3 * 1000;
const now = Date.now();
const state = {{ selected: '%1', currentBlocks: [], optimisticUserPromptsByPane: new Map() }};
function normalizePromptForMatch(value) {{ return String(value || '').replace(/\\s+/g, ' ').trim(); }}
{parse_start}
{pending}
{response}
{awaits}
const fresh = {{ pane_id:'%1', job:{{source:'card-dashboard',status:'running',task_preview:'检查',created_at:new Date(now-5000).toISOString()}} }};
if (!paneAwaitsReply(fresh, now)) throw new Error('fresh unanswered turn should wait');
const old = {{ pane_id:'%1', job:{{...fresh.job,created_at:new Date(now-2*60*60*1000).toISOString()}} }};
if (paneAwaitsReply(old, now)) throw new Error('ledger wait exceeded ceiling');
const started = {{ pane_id:'%1', job:{{...fresh.job,response_started_at:new Date(now-1000).toISOString()}} }};
if (paneAwaitsReply(started, now)) throw new Error('durable first response did not stop waiting');
state.currentBlocks = [{{role:'user',text:'检查'}},{{role:'assistant',text:'我先核对。'}}];
if (paneAwaitsReply(fresh, now)) throw new Error('visible first response did not stop waiting');
"""
        self.run_node(script)

    def test_response_started_banner_disappears_on_final_block(self) -> None:
        started = function_source(self.source, "paneHasStartedResponse", "paneAwaitsReply")
        visible = function_source(self.source, "paneHasVisibleResponse", "paneShowsThinking")
        normalize = function_source(self.source, "normalizePromptForMatch", "addOptimisticUserPrompt")
        script = f"""
const state = {{ selected: '%1', optimisticUserPromptsByPane: new Map() }};
function hasHumanSummary() {{ return false; }}
{normalize}
{visible}
{started}
const pane = {{pane_id:'%1', job:{{source:'card-dashboard', status:'running', task_preview:'检查', response_started_at:'2026-09-02T00:00:00Z'}}}};
const blocks = [{{role:'user', text:'检查'}}, {{role:'assistant', text:'完成', final:true}}];
        if (paneHasStartedResponse(pane, blocks)) throw new Error('final response kept response-started banner');
"""
        self.run_node(script)

    def test_live_running_pane_keeps_bottom_processing_indicator_visible(self) -> None:
        """A previous final answer must not hide the current live turn's row."""
        thinking = function_source(self.source, "paneShowsThinking", "markPaneRunning")
        script = f"""
const state = {{ thinkingSince: {{}}, lastThinkingSendAtByPane: {{}} }};
function paneIsRunning(pane) {{ return pane.status === 'running'; }}
function paneAwaitsReply() {{ return false; }}
function paneHasVisibleResponse() {{ return true; }}
{thinking}
const pane = {{pane_id:'%1', status:'running'}};
if (!paneShowsThinking(pane, [{{role:'assistant', final:true, text:'previous answer'}}]))
  throw new Error('live processing row was hidden by an older final answer');
"""
        self.run_node(script)

    def test_processing_state_does_not_trust_running_ledger_alone(self) -> None:
        source = function_source(self.source, "paneIsProcessing", "paneIsActivelyThinking")
        self.assertNotIn('String(pane.job?.status || "") === "running"', source)
        self.assertIn("paneAwaitsReply(pane)", source)

    def test_continued_work_copy_has_no_wait_timer(self) -> None:
        source = function_source(self.source, "renderThinking", "renderBlockBody")
        self.assertIn("已回复，继续处理中", source)
        self.assertIn("不再按“等待回复”累计时间", source)
        self.assertIn("本轮请求处理中", source)
        self.assertIn("本轮：", source)
        response_branch = source.split("if (responseStarted)", 1)[1].split("const started", 1)[0]
        self.assertNotIn("thinking-time", response_branch)

    def test_job_pill_calls_active_responded_turn_replied(self) -> None:
        source = function_source(self.source, "jobPill", "jobMetaValue")
        self.assertIn('responseStarted ? "处理中·已有回复"', source)
        self.assertNotIn('responseStarted ? "已回复"', source)
        self.assertIn('" response-started"', source)

    def test_terminal_ledger_row_cannot_hide_live_pane_work(self) -> None:
        effective = function_source(self.source, "effectiveJobStatus", "jobPill")
        pill = function_source(self.source, "jobPill", "jobMetaValue")
        self.assertIn("paneIsRunning(pane)", effective)
        self.assertIn('return "running"', effective)
        self.assertIn("effectiveJobStatus(job, pane)", pill)
        self.assertIn('const live = pane && paneIsRunning(pane) ? " live" : ""', pill)
        script = f"""
const THINKING_JOB_STATUSES = new Set(['created','sent','queued','leased','starting','running']);
function paneIsRunning(pane) {{ return Boolean(pane && pane.status === 'running'); }}
{effective}
        if (effectiveJobStatus({{status:'completed'}}, {{status:'running'}}) !== 'running') throw new Error('live pane was shown completed');
        if (effectiveJobStatus({{status:'completed'}}, {{status:'idle'}}) !== 'completed') throw new Error('idle pane lost completed status');
if (effectiveJobStatus({{status:'completed'}}, {{status:'idle', identity_fidelity:'ambiguous'}}) !== 'identity-ambiguous') throw new Error('ambiguous identity was shown completed');
"""
        self.run_node(script)

    def test_capture_loading_uses_elapsed_time_not_fake_percentage(self) -> None:
        loading = function_source(self.source, "renderSwipeLoading", "setTimelineCacheNotice")
        starter = function_source(self.source, "startCaptureLoading", "finishCaptureLoading")
        self.assertIn("captureLoadingElapsed", loading)
        self.assertIn("data-capture-retry", loading)
        self.assertNotIn("captureLoadingPercent", loading)
        self.assertNotIn("Math.min(88", starter)
        self.assertNotIn("%", starter)

    def test_capture_request_times_out_and_newer_request_cancels_old_one(self) -> None:
        fetch_capture = function_source(self.source, "fetchLatestCapture", "pollEvents")
        script = f"""
const CAPTURE_REQUEST_TIMEOUT_MS = 12000;
const state = {{ captureAbortController: null, captureRequestSeq: 1 }};
function abortError() {{
  const error = new Error('aborted');
  error.name = 'AbortError';
  return error;
}}
global.fetch = (url, options) => {{
  if (url === '/fast') {{
    return Promise.resolve({{ ok: true, status: 200, json: async () => ({{ fresh: true }}) }});
  }}
  return new Promise((_resolve, reject) => {{
    options.signal.addEventListener('abort', () => reject(abortError()), {{ once: true }});
  }});
}};
{fetch_capture}
(async () => {{
  const oldRequest = fetchLatestCapture('/slow-old', 1, 1000);
  state.captureRequestSeq = 2;
  const freshRequest = fetchLatestCapture('/fast', 2, 1000);
  if (await oldRequest !== null) throw new Error('superseded request was not discarded');
  const fresh = await freshRequest;
  if (!fresh?.fresh) throw new Error('new request did not win');

  state.captureRequestSeq = 3;
  let timeoutMessage = '';
  try {{
    await fetchLatestCapture('/slow-timeout', 3, 5);
  }} catch (error) {{
    timeoutMessage = error.message;
  }}
  if (!timeoutMessage.includes('读取最新记录超过')) {{
    throw new Error(`missing friendly timeout: ${{timeoutMessage}}`);
  }}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        self.run_node(script)

    def test_user_facing_name_is_cards_website(self) -> None:
        self.assertIn("<title>卡片网站</title>", self.source)
        self.assertIn("<h1>卡片网站</h1>", self.source)
        self.assertIn('document.title = "卡片网站"', self.source)

    def test_page_does_not_request_a_missing_favicon(self) -> None:
        self.assertIn('<link rel="icon" href="data:,">', self.source)

    def test_refresh_loop_does_not_cancel_an_active_pane_switch_capture(self) -> None:
        start = self.source.index("    async function refreshLoop(")
        end = self.source.index("\n    applyFontSize();", start)
        refresh_loop = self.source[start:end]
        self.assertIn("paneSwitchCaptureInFlight", refresh_loop)
        self.assertIn("Boolean(state.captureAbortController)", refresh_loop)
        self.assertIn("!paneSwitchCaptureInFlight", refresh_loop)


if __name__ == "__main__":
    unittest.main()
