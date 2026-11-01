"""Shared interaction policy for lead-agent tools and prompt guidance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"


class RunInteractionMode(StrEnum):
    """Interaction modes that can be selected by a trusted run entry point."""

    INTERACTIVE = "interactive"
    AUTONOMOUS = "autonomous"
    WEBHOOK = "webhook"
    SCHEDULED = "scheduled"


_INTERACTIVE_CLARIFICATION_SYSTEM = """<clarification_system>
**ASK THE USER WHEN INPUT WOULD HELP — AT ANY STAGE**

`ask_clarification` is the tool for asking the user questions throughout the task.
You may pause during reasoning, planning, research, execution, or review to ask for
information, preferences, a decision, confirmation, or feedback. Questions are not
limited to clarifying the initial request, and you do not need to be completely
blocked before asking. New findings can raise new questions after work has begun.

**When to ask:**
- Ask when a question arises and the user's answer would meaningfully guide the
  next step, such as conflicting findings, an unexpected constraint, or a useful
  improvement whose scope or priority needs user input.
- If required information or authorization is missing, ask before taking the
  action that depends on it. You may already have completed other work.
- Use available context and previous answers; do not ask the user to repeat a
  decision or reconfirm an action they have already authorized. Handle routine,
  low-impact details yourself when no meaningful user preference is involved.

**Choose the closest question type** (`clarification_type`):
- `missing_info`: Request facts, constraints, or details, including gaps discovered during work.
- `ambiguous_requirement`: Ask how to interpret an unclear goal, requirement, or conflicting information.
- `approach_choice`: Ask the user to choose a direction, tradeoff, priority, or next step.
- `risk_confirmation`: Request necessary confirmation before a risky action without sufficient authorization.
- `suggestion`: Propose an improvement or adjustment and ask for the user's opinion or approval.

**How to ask and continue:**
- Ask a concise, specific question. Briefly explain relevant findings, what is
  uncertain, and how the answer will affect the next step; do not expose private reasoning.
- Focus on one decision at a time. Use `options` for a single choice, or `fields`
  to collect related values together (including a `multi_select` field for multiple choices).
- Call `ask_clarification` on its own: do not call other tools in the same turn;
  sibling tool calls are dropped. Execution stops automatically to present the question.
- Wait for the user's answer. Do not treat silence as an answer or continue with
  an assumption about the pending question.
- After the user replies, incorporate the answer and resume from the paused step,
  preserving completed work. Ask again if a new, distinct question arises later.

**How to Use:**
```python
ask_clarification(
    question="Your specific question here?",
    clarification_type="missing_info",
    context="Relevant findings and how the answer will guide the next step",
    options=["option1", "option2"]
)
```

**Example — a question discovered during execution:**
User: "Analyze this quarter's sales."
After inspecting the data, you find both order-date and payment-date totals.
ask_clarification(
    question="Should this quarter's sales be counted by order date or payment date?",
    clarification_type="approach_choice",
    context="The data contains both dates; the choice changes which sales belong to this quarter.",
    options=["Order date", "Payment date"]
)
[Execution stops - wait for user response]

User: "Payment date"
Continue the analysis using payment dates and the data already inspected.
</clarification_system>"""

_INTERACTIVE_CLARIFICATION_SYSTEM_ZH = """<clarification_system>
**在任务的任何阶段，只要用户的回答有助于推进，就可以询问**

`ask_clarification` 用于在整个任务过程中向用户提问。你可以在推理、规划、调研、
执行或检查结果时暂停，询问信息、偏好、决定、确认或反馈。提问不限于开始工作前
澄清需求，也不必等到完全无法继续；工作中的新发现同样可能带来新的疑问。

**何时询问：**
- 产生疑问且用户的回答能实质影响下一步时，可以询问。例如发现资料冲突、
  意外约束，或有值得提出但需要用户决定范围或优先级的改进建议。
- 缺少必要信息或授权时，先询问，再执行依赖该回答的操作；此前可以已经完成其他工作。
- 复用现有上下文及用户回答，不让用户重复做决定或确认已经授权的操作。
  不涉及重要用户偏好的常规、低影响细节，可以自行处理。

**选择最贴近的询问类型**（`clarification_type`）：
- `missing_info`：补充事实、约束或细节，包括工作中才发现的信息缺口。
- `ambiguous_requirement`：确认不明确的目标、需求或冲突资料应如何理解。
- `approach_choice`：请用户选择方向、权衡、优先级或下一步。
- `risk_confirmation`：在缺少充分授权的风险操作前，请用户进行必要的确认。
- `suggestion`：提出改进或调整建议，征求用户意见或批准。

**如何提问并继续：**
- 问题简短具体，简述相关发现、疑问及回答对下一步的影响，不展示内部推理过程。
- 一次聚焦一个决定。单选使用 `options`；收集多个相关值使用 `fields`，
  多选使用 `type="multi_select"` 的表单字段。
- 单独调用 `ask_clarification`，同一轮不要调用其他工具；同轮其他工具调用会被丢弃。
  执行会自动停止并向用户展示问题。
- 等待用户回答，不把沉默当作回答，也不自行假设待回答问题的答案后继续。
- 收到回答后结合已有进展从暂停处继续，保留已完成的工作。
  后续出现不同的新问题时，仍可再次询问。

**调用方式：**
```python
ask_clarification(
    question="需要用户回答的具体问题？",
    clarification_type="missing_info",
    context="相关发现，以及回答将如何影响下一步",
    options=["选项一", "选项二"]
)
```

**示例——执行过程中发现问题：**
用户：“分析本季度销售情况。”
检查数据后，发现同时存在按下单日期和付款日期统计的销售额。
ask_clarification(
    question="本季度销售额应按下单日期还是付款日期统计？",
    clarification_type="approach_choice",
    context="数据中同时包含这两种日期，选择不同会影响哪些销售记录计入本季度。",
    options=["下单日期", "付款日期"]
)
[执行停止，等待用户回答]

用户：“付款日期。”
基于已经检查的数据，按付款日期继续分析。
</clarification_system>"""

_AUTONOMOUS_CLARIFICATION_SYSTEM = """<clarification_system>
**WORKFLOW PRIORITY: ASSESS -> CHOOSE THE LOWEST-RISK PATH -> ACT**

There is no human available to answer a synchronous question during this run.
Do not wait for clarification or approval. Resolve ambiguity from the request,
{context_sources}.

- For low-risk and reversible work, make the smallest reasonable assumption and continue.
- State every material assumption in the final result, one line each: what was unclear, what you chose, and why.
- For high-risk or irreversible work without sufficient authorization, do not guess:
  stop with a concise structured `BLOCKED` result that names the missing decision.
- Prefer inspection and read-only checks before changing state.
</clarification_system>"""

_AUTONOMOUS_CONTEXT_SOURCES = "the available run context and existing configuration"
_WEBHOOK_CONTEXT_SOURCES = "the issue, pull request, repository, event context, and existing configuration"


@dataclass(frozen=True, slots=True)
class RunInteractionPolicy:
    """The single source for interaction-sensitive tools and prompt guidance."""

    mode: RunInteractionMode

    @property
    def allows_clarification(self) -> bool:
        return self.mode is RunInteractionMode.INTERACTIVE

    @property
    def disabled_tool_names(self) -> frozenset[str]:
        if self.allows_clarification:
            return frozenset()
        return frozenset({ASK_CLARIFICATION_TOOL_NAME})

    @property
    def thinking_guidance(self) -> str:
        if self.allows_clarification:
            return "- **USER INPUT CHECK: During reasoning or work, use `ask_clarification` whenever the user's information, preference, decision, or feedback would meaningfully guide the next step. You may pause and ask at any stage, then resume after the answer.**"
        return "- **INTERACTION CHECK: This run has no synchronous human. Resolve ambiguity with the run context, choose the lowest-risk reversible path, and record material assumptions.**"

    @property
    def clarification_system(self) -> str:
        if self.allows_clarification:
            return _INTERACTIVE_CLARIFICATION_SYSTEM
        context_sources = _WEBHOOK_CONTEXT_SOURCES if self.mode is RunInteractionMode.WEBHOOK else _AUTONOMOUS_CONTEXT_SOURCES
        return _AUTONOMOUS_CLARIFICATION_SYSTEM.format(context_sources=context_sources)

    @property
    def clarification_reminder(self) -> str:
        if self.allows_clarification:
            return "- **Ask Throughout the Task**: Use `ask_clarification` for questions before or during work; pause when asking, wait for the answer, and resume from the paused step. Reuse prior answers and authorization."
        return "- **Autonomous Interaction**: Do not wait for a human response; make minimal reversible assumptions, list them, or return a structured `BLOCKED` result for high-risk ambiguity"

    def prompt_sections(self, language: str = "en") -> dict[str, str]:
        if language == "zh" and self.allows_clarification:
            return {
                "interaction_thinking_guidance": "- **用户输入检查：推理或工作过程中，只要用户的信息、偏好、决定或反馈能实质帮助确定下一步，就可以使用 `ask_clarification`。任何阶段都可以暂停询问，收到回答后继续。**",
                "clarification_system": _INTERACTIVE_CLARIFICATION_SYSTEM_ZH,
                "clarification_reminder": "- **全程可询问**：开始工作前或执行中有疑问，都可以使用 `ask_clarification`；提问后暂停并等待回答，再从暂停处继续。复用已有回答和授权。",
            }
        return {
            "interaction_thinking_guidance": self.thinking_guidance,
            "clarification_system": self.clarification_system,
            "clarification_reminder": self.clarification_reminder,
        }

    @classmethod
    def interactive(cls) -> RunInteractionPolicy:
        return cls(RunInteractionMode.INTERACTIVE)


def resolve_run_interaction_policy(config: Mapping[str, Any] | None) -> RunInteractionPolicy:
    """Resolve one policy from legacy runtime flags and channel context.

    ``non_interactive`` is the scheduler's trusted legacy flag. GitHub and
    other webhook channels currently use ``disable_clarification`` and/or
    ``channel_name``; both remain supported while callers migrate to an
    explicit mode.
    """

    merged: dict[str, Any] = {}
    if config:
        configurable = config.get("configurable")
        context = config.get("context")
        if isinstance(configurable, Mapping):
            merged.update(configurable)
        if isinstance(context, Mapping):
            merged.update(context)

    raw_mode = merged.get("interaction_mode")
    if raw_mode is not None:
        try:
            return RunInteractionPolicy(RunInteractionMode(str(raw_mode)))
        except ValueError as exc:
            valid_modes = ", ".join(mode.value for mode in RunInteractionMode)
            raise ValueError(f"Invalid interaction_mode {raw_mode!r}; expected one of: {valid_modes}") from exc

    if merged.get("non_interactive"):
        return RunInteractionPolicy(RunInteractionMode.SCHEDULED)
    if merged.get("channel_name") == "github":
        return RunInteractionPolicy(RunInteractionMode.WEBHOOK)
    if merged.get("disable_clarification"):
        return RunInteractionPolicy(RunInteractionMode.AUTONOMOUS)
    return RunInteractionPolicy.interactive()
