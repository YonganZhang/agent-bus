#!/usr/bin/env python3
"""按 provider 分开的屏幕结构识别。

每种 AI CLI 的界面长得不一样，但它们都有同一个东西：**输入区**——屏幕底部那块会被
就地重绘的、表示"我在等你打字"的结构。Claude 是两条 ─ 夹着 ❯ 的输入框加底部 chrome，
Codex 是 `› Ask Codex to do anything` 加 model 行。

把这件事按 provider 拆开，是因为它是唯一会随上游版本漂移的部分：Claude Code 改一次
界面文案，坏的只应该是 ClaudeScreen 这一个类和它的 fixture，而不是一个混在一起、
谁也不敢动的大函数。新增一种 CLI 也只是多一个类加一份 fixture。

结构必须在内容过滤之前识别：清理噪音那一步会把边框和 chrome 当垃圾剥掉，那之后就再也
没法回答"这个会话是不是停在输入框等人"，只能退回去猜正文里的词——而正文里出现
`error:`、贴段 traceback、或者 Claude 播报一句后台命令 failed，猜就会翻车。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 底边永远是一条干净的横线。
PROMPT_BOX_RULE_RE = re.compile(r"^\s*[─━═—]{10,}\s*$")
# 上边常在右端挂着项目名 —— `────── demo-session-title ─`。窗口越窄,
# 项目名占得越多、前导横线越短(实测最短只剩 6 个),所以这里认的是"横线开头 + 横线结尾"
# 这个形态,而不是横线的数量。误命中由"必须紧挨在底边上方"和 MAX_INPUT_BOX_HEIGHT 挡住。
PROMPT_BOX_TOP_RE = re.compile(r"^\s*[─━═—]{3,}.*[─━═—]\s*$")
PROMPT_CARET_RE = re.compile(r"^\s*❯")
CODEX_PROMPT_RE = re.compile(r"^\s*›\s*Ask Codex to do anything", re.I)
# 输入框很矮(边框+提示符+可能几行已输入内容)。限高是为了不把对话区里更早的一条
# 分隔线错当成上边框,把半屏对话吞进输入区。
# 用户正在敲的多行消息会把输入框撑高。定太小(原来是 10)的后果是:消息一超过九行,
# 上边框就落在窗口外找不着,结构判断悄悄失效、退回关键词匹配 —— 正是这次要消灭的那种
# 误报。输入框必须紧贴屏幕底部已由 MAX_LINES_BELOW_ANCHOR 单独保证,所以这里可以放宽。
MAX_INPUT_BOX_HEIGHT = 16
# 输入区必须真的长在屏幕底部。锚点可以出现在历史滚动里(上一屏的输入框、别人贴的
# 一段 Codex 界面),那种是残影不是"此刻在等你打字"。实测 53 份 fixture 加 58 个活
# pane,锚点下方最多只有 4 行非空内容,取 8 留足余量。
# 不设这个约束的后果:一个已经死掉的 AI 留在屏幕上的输入框会让状态永远判成 idle,
# 把 needs attention 告警整个杀掉。
# 实测 53 份 fixture 加 58 个活 pane,锚点下方的非空行数只出现过 1/3/4 三个值。取 4:
# 放到 5 或 6,一个已经死掉的 Codex 会话留在屏幕上的输入框就又会把状态压成 idle
# (审查实测)。Codex 是 inline 渲染(alternate_on=0),进程死了那一屏还留在 scrollback 里,
# 所以这个约束对 Codex 尤其要紧。
MAX_LINES_BELOW_ANCHOR = 4


# Claude Code 有子智能体在跑时，会在输入框下方的状态栏后面再挂一块面板："● main"
# 加上每个子智能体一行 "◯ fast-worker   在干什么"，行数随子智能体个数变。它和状态栏
# 一样属于输入区的 chrome，不算"锚点下方的内容"；否则十个子智能体就能把输入框"顶"
# 出底部，整屏退回去猜正文（面板会被当成一条 AI 回复重复显示，
# "Waiting for 10 background agents" 也会被挤出状态判断看的那几行）。
# 只认缩进的行：对话区里 AI 回复的 "● " 顶格写，不会被误吞。
AGENT_PANEL_ROW_RE = re.compile(r"^\s{1,6}[●◯○◉]\s+\S")


def strip_agent_panel(raw_lines: list[str]) -> list[str]:
    """去掉屏幕最底部的子智能体面板行（连同夹在其中的空行）。"""
    end = len(raw_lines)
    while end > 0 and (not raw_lines[end - 1].strip() or AGENT_PANEL_ROW_RE.match(raw_lines[end - 1])):
        end -= 1
    return raw_lines[:end]


def _anchor_is_at_bottom(raw_lines: list[str], index: int) -> bool:
    below = strip_agent_panel(raw_lines[index:])
    return sum(1 for line in below if line.strip()) <= MAX_LINES_BELOW_ANCHOR + 1


@dataclass(frozen=True)
class ScreenSplit:
    """一屏被切成的两段。`input_lines` 为空表示这屏没有输入区。"""

    conversation: list[str] = field(default_factory=list)
    input_lines: list[str] = field(default_factory=list)
    provider: str = ""

    @property
    def has_input_region(self) -> bool:
        return bool(self.input_lines)


class ClaudeScreen:
    """Claude Code：对话区 + `─`/`❯`/`─` 输入框 + 底部 chrome。

    从下往上找第二条横线才是输入框顶边——第一条是底边。对话区里也会出现横线
    (`──── project-name ─` 这类项目标识)，但那种带文字，匹配不上纯横线。
    """

    name = "claude"

    def split(self, raw_lines: list[str]) -> ScreenSplit | None:
        # 从最下面往上,第一条干净的横线就是输入框底边。
        bottom = -1
        for index in range(len(raw_lines) - 1, -1, -1):
            if PROMPT_BOX_RULE_RE.match(raw_lines[index]):
                bottom = index
                break
        if bottom <= 0 or not _anchor_is_at_bottom(raw_lines, bottom):
            return None

        # 优先认上边框:它才是输入框真正的起点,认它才能把边框本身归进输入区。
        for index in range(bottom - 1, max(-1, bottom - MAX_INPUT_BOX_HEIGHT - 1), -1):
            if PROMPT_BOX_TOP_RE.match(raw_lines[index]):
                return ScreenSplit(raw_lines[:index], raw_lines[index:], self.name)

        # 上边框没画出来时(窄窗口偶发)再退一步:底边正上方紧挨着的 ❯ 同样能证明
        # 这是输入框。少认一个输入框,整个会话就会被拿去猜正文里的词,代价比偶尔多认大得多。
        for index in range(bottom - 1, max(-1, bottom - 4), -1):
            if PROMPT_CARET_RE.match(raw_lines[index]):
                return ScreenSplit(raw_lines[:index], raw_lines[index:], self.name)
        return None


class CodexScreen:
    """Codex：没有边框，输入区从 `› Ask Codex to do anything` 那行开始。"""

    name = "codex"

    def split(self, raw_lines: list[str]) -> ScreenSplit | None:
        for index in range(len(raw_lines) - 1, -1, -1):
            if not CODEX_PROMPT_RE.match(raw_lines[index]):
                continue
            if not _anchor_is_at_bottom(raw_lines, index):
                return None
            return ScreenSplit(raw_lines[:index], raw_lines[index:], self.name)
        return None


# 顺序即优先级：先试文案锚点明确的，再试靠边框推断的。Codex 的锚点是一句固定英文，
# 比"两条横线"这种纯几何特征更不容易误命中。
DETECTORS: tuple[ClaudeScreen | CodexScreen, ...] = (CodexScreen(), ClaudeScreen())


def split_screen(raw_lines: list[str]) -> ScreenSplit:
    for detector in DETECTORS:
        result = detector.split(raw_lines)
        if result is not None:
            return result
    return ScreenSplit(list(raw_lines), [], "")
