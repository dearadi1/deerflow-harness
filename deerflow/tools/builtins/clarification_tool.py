from typing import Literal, Required, TypedDict

from langchain.tools import tool


class ClarificationFormField(TypedDict, total=False):
    """用于向用户收集信息的结构化表单字段。

    此结构为模型提供字段说明；中间件会在工具执行前拦截调用，
    因此运行时校验仍由 ``ClarificationMiddleware`` 负责。
    """

    name: Required[str]
    label: str
    type: Literal["text", "textarea", "number", "select", "multi_select", "checkbox", "date"]
    required: bool
    options: list[str]
    placeholder: str


@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
    fields: list[ClarificationFormField] | None = None,
) -> str:
    """向用户询问信息、偏好、决定、确认或反馈，并暂停当前执行等待回答。

    可在推理、规划、调研、执行或检查结果的任何阶段使用，不限于开始工作前澄清需求。
    过程中产生疑问、新发现或方案分歧，只要用户的回答有助于确定下一步，就可以临时
    停下来询问；无需等到完全无法继续。收到回答后，结合已有进展从暂停处继续。

    适用情形：
    - 需要补充事实、约束或参数，包括工作中才发现的信息缺口。
    - 发现需求或资料存在多种解释，希望确认用户的意图。
    - 需要用户选择方向、权衡、优先级或下一步。
    - 即将执行需要确认且尚未得到充分授权的风险操作。
    - 有新的建议或调整方案，希望听取用户意见或获得批准。

    提问方式：
    - 开放问题：使用 `question`；单选问题：增加 `options`。
    - 多选问题：使用一个 `type="multi_select"` 的 `fields` 字段。
    - 一次收集多个相关值：使用 `fields` 表单，避免逐字段反复打断用户。
    - 一次聚焦一个决定，问题简短具体；用 `context` 说明当前发现、疑问和回答的影响，
      不展示内部推理过程。
    - 复用已有信息、用户回答及授权，不重复询问；不涉及重要用户偏好的常规细节可自行处理。
    - 缺少必要信息或授权时，先询问，再执行依赖该回答的操作。
    - 技能提供预定义字段模板时，原样通过 `fields` 传入。
    - 调用后执行会自动中断；同一轮不得调用其他工具，同轮其他工具调用会被丢弃。
    - 等待用户回答，不把沉默当作同意，也不自行假设待回答问题的答案。

    Args:
        question: 展示给用户的具体问题，明确希望对方提供什么信息或做什么决定；可以是执行过程中产生的新问题。
        clarification_type: 询问目的，选择最贴近的一项：missing_info 补充信息；ambiguous_requirement 确认含义或意图；
            approach_choice 选择方向、方案或优先级；risk_confirmation 确认尚未充分授权的风险操作；suggestion 征求对建议或调整的意见。
            这些类型均可用于任务开始前或执行过程中。
        context: 可选的简短背景，说明已发现的事实、当前疑问以及回答将如何影响下一步；不重复整个任务或展开内部推理。
        options: 可选的单选列表，适用于任意询问类型；选项应清晰、易区分，用户也可自由输入。需要多选时改用 fields。
        fields: 可选的表单字段列表，用于一次收集多个相关值或提供多选；有效且非空时优先于 options。
            每个字段包含 name（必填的唯一标识，避免 constructor、toString 等 JavaScript 原型保留名）、
            label（展示文字，默认使用 name）、type（text、textarea、number、select、multi_select、checkbox、date，
            默认 text）、required（是否必填，默认 false）、options（select/multi_select 必须提供的字符串列表）
            和 placeholder（可选输入提示）。checkbox 是布尔值，默认表示“否”；仅在必须勾选同意时设 required=true。
            最多 16 个字段、每字段 24 个选项，name/label/option/placeholder 每项最多 200 字符，
            归一化表单 JSON 最多 16384 UTF-8 字节；超过限制会退回普通单选或自由文本提问。
    """
    # 由中间件拦截并结束本轮执行，避免展示问题后继续执行依赖用户回答的操作。
    return "Clarification request processed by middleware"
