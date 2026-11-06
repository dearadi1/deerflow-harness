# 每轮自动选择模型和执行模式

`deerflow.agent_client` 的 `pool.start_stream()`、session 的 `stream()` / `chat()`
支持 `auto_routing_enabled: bool = False`。仅当请求参数与
`config.yaml -> auto_routing.enabled` 同时开启时调用分析模型。

```python
from deerflow.agent_client import DeerFlowClientPool

with DeerFlowClientPool(size=2, config_path="config.yaml") as pool:
    with pool.start_stream(
        "比较两种方案，并制定实施步骤",
        user_id="user_001",
        thread_id="thread_001",
        auto_routing_enabled=True,
    ) as events:
        for event in events:
            print(event.agent_setting)
            print(event.model_dump_json())
```

参数只对本轮生效，下次省略恢复手动选择。`chat()` 仍返回最终回答字符串；需要
设置和用量时使用流式入口。修改配置后重新加载配置并重建池，例如传入 `config_path`；
仅失效 Agent 缓存不会更新客户端持有的应用配置快照。

## 配置

```yaml
auto_routing:
  enabled: false
  model_name: null
  prompt: null
  timeout_seconds: 8
  max_output_tokens: 768
  routes:
    fast: {text: null, vision: null}
    thinking: {text: null, vision: null}
    plan: {text: null, vision: null}
    subagent: {text: null, vision: null}
  context:
    recent_turns: 4
    input_token_budget: 4096
    expanded_input_token_budget: 8192
    summary_token_budget: 512
    max_analysis_calls: 2
  fallback_mode: thinking
```

`model_name` 填分析模型的 `models[].name`，建议选低延迟模型，留空使用第一个可用
模型。`routes` 映射档位和视觉需求到执行模型。名称未填写、不存在、能力不兼容或
权限不允许时，从已配置且获授权的模型回退，优先满足视觉与思考能力，再采用该档位
及兜底档位的配置模型。`prompt` 替换内置分类指导语，框架仍追加固定 JSON 输出约束。

自动路由生效后，本轮及客户端默认的 `model_name`、`thinking_enabled`、
`plan_mode` / `is_plan_mode`、`subagent_enabled` 不参与选择。全局关闭或请求
未开启时使用手动配置来源。

| 档位 | thinking_enabled | is_plan_mode | subagent_enabled |
| --- | --- | --- | --- |
| fast | false | false | false |
| thinking | true | false | false |
| plan | true | true | false |
| subagent | true | true | true |

所有实际设置，包括手动配置，都按模型能力归一化：规划同时请求思考，子代理同时
请求规划和思考；模型不支持思考时三个开关均关闭，要求始终思考时保持开启。
这会调整旧调用中相互矛盾的开关组合，不因思考能力不匹配直接报错。

工具禁用、模型权限及 named agent 的子代理禁用仍有效。`task` / `batch_task`
均不可用时回传 `subagent_enabled=false`。规划模式也不绕过 `disabled_tools`。
分析超时、异常或无效 JSON 使用兜底档位；明确延续旧任务且信息不足时，至少保留
上一轮档位。没有视觉模型时回退文本配置并记录日志，文本模型不会因此具备看图能力。
当前视觉路由依据 `supports_vision`，音频、视频能力不等同于视觉能力。
无模型、无使用权限仍沿用配置/权限错误，回退不会授予权限。

## 长上下文

输入由当前问题、最近几轮用户/最终回答、路由任务摘要、已有会话摘要、待办/目标和
附件元数据组成。跳过系统消息、隐藏注入、推理块、工具调用及工具正文；不发送图片
Base64 或完整文件正文。PII 配置同样作用于分析输入，先脱敏再截断。

预算包含提示词与 JSON 封装。采用 UTF-8 字节上界加消息余量保守估算 Token，避免
在线下载分词器和低估中文；它比供应商真实 Token 数更保守，不用于计费。超长内容
保留首尾并标记截断。通常调用一次；仅当缺少历史引用时，从最多 256 条历史消息中
检索相关片段并扩充一次。两次输入分别受限，调用共享总超时，仍不足时按兜底选档。

同次分析产生短任务摘要，随输入保存至 `ThreadState.auto_routing`，后续结合最新
回答和待办更新，新话题替换摘要，不增加独立摘要调用。手动轮次清空路由摘要；再次
启用时依靠近期历史与会话摘要重建。此视图不裁剪主 Agent 的正常历史。
有限视图无法恢复所有旧细节；底层 checkpoint 仍需物化，预算限制的是模型输入，
并非数据库读取量。

## 流设置与 Token、成本

每个新事件增加与 `thread_id` 同级的 `agent_setting`：

```json
{
  "type": "answer_content",
  "agent_id": "main",
  "data": {"id": "message_001", "content": "回答"},
  "thread_id": "thread_001",
  "agent_setting": {
    "model": "configured_model_name",
    "thinking_enabled": true,
    "is_plan_mode": true,
    "subagent_enabled": false
  },
  "seq": 1
}
```

它描述本轮主 Agent 的实际配置，同轮固定，手动请求也返回。子代理事件携带相同的
轮次设置，具体调用模型由 `token_usage.data.model_name` 标识。早期未完成设置
解析的错误可为 `null`；解析器兼容旧事件缺少该字段。旧版严格 Schema 需同步升级。

分析计费复用现有 `ClientStreamParser` / `ClientStreamPricing` 流程：

- 每次分析发布独立 `token_usage`，`data.model_name` 是实际分析模型的配置名称。
- 按该模型的 `models[].price` 分别计算普通输入、缓存命中输入、输出费用，沿用
  人民币/百万 Token 单价及十进制金额约定。
- 两次分析均计入主 `end` 的 Token 与费用汇总；无效 JSON 已消耗的用量也保留。
- 分析 JSON 不进入回答或历史。分析用量先于主 Agent 事件发布，共用连续 `seq`。
- 供应商未返回用量时不估算、不补零；已发起但超时/失败的分析调用保留身份，使
  费用汇总标记 `unpriced`。缺少价格沿用现有无法计价规则。

外层 `agent_setting.model` 始终是本轮主模型，不能用它给分析调用计费。
`end` 只汇总已获得的真实用量，费用不完整时不返回虚假的完整总价。

## 生命周期与验证

`agents/auto_routing.py` 提供创建 Agent 前的独立预处理函数。现有
`AgentMiddleware` hook 在图创建后执行，因此选择结果交给原有模型工厂、缓存及
中间件组装流程，决定本轮规划和子代理能力。

读取历史前校验线程归属；重生成读取被替换轮次之前的基线并遵循附件继承规则。
路由不提前改写 checkpoint。托管入口复用本轮取消机制，`close()` 取消原生异步
分析并等待退出，再归还池容量。

测试从 `deerflow.agent_client` 进入，使用真实图、客户端池和内存存储，模型及
环境服务采用离线替身。`full` / `delta` 分进程运行：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_agent_client_auto_routing.py' -v
DEERFLOW_ROUTING_TEST_MODE=delta .venv/bin/python -m unittest discover -s tests -p 'test_agent_client_auto_routing.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_auto_routing.py' -v
```

覆盖组装、参数覆盖、能力回退、独立计费及汇总、预算、取消、重生成和用户隔离。
真实供应商的分类准确率与延迟需使用部署选择的模型另行评估。
