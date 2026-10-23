# Metaso 网络检索与网页获取

本目录通过 Metaso HTTP API 提供 `web_search` 和 `web_fetch`，实现位于
`tools.py`。保持与 `../tavily/tools.py` 一致的异步工具、配置读取和 JSON
序列化风格；输出格式遵守本模块约定。

## 工具接口

- `async def web_search_tool(query: str, scope: str = "webpage") -> str`
- `async def web_fetch_tool(url: str) -> str`

两者均使用 `@tool(..., parse_docstring=True)`。模型可见参数仅限上述参数；
数量、摘要、原文、超时及凭据等设置从 `config.yaml` 对应工具配置的
`model_extra` 读取，不增加到工具签名中。内部注释和 docstring 使用中文；
按用户要求，两个工具面向模型的说明与参数描述使用英文，保留 Google 风格的
`Args:` 段落，确保 LangChain 能解析描述并序列化参数 schema。

## 请求与配置

- 检索：`POST https://metaso.cn/api/v1/search`，`Accept: application/json`。
- 获取：`POST https://metaso.cn/api/v1/reader`，`Accept: text/plain`，请求体为 `{"url": url}`。
- 请求体均使用 JSON，认证为 `Authorization: Bearer <api_key>`。
- 按工具名分别读取 `web_search`、`web_fetch` 的配置，不能交叉使用凭据。
- 优先使用配置中的 `api_key`，未配置时读取 `METASO_API_KEY` 环境变量。
  `.env` 由应用配置加载流程处理，不在工具内重复加载；示例中使用
  `api_key: $METASO_API_KEY`，不得写入真实密钥。

| 配置项 | 适用工具 | 默认值 | 行为 |
| --- | --- | --- | --- |
| `api_key` | 两者 | 环境变量 | 工具专用凭据 |
| `timeout` | 两者 | `30.0` | HTTPX 超时，单位为秒 |
| `max_results` | 检索 | `10` | 正整数或可转换为正整数的字符串，以字符串 `size` 发送 |
| `include_summary` | 检索 | `false` | 对应 `includeSummary` |
| `include_raw_content` | 检索 | `false` | 对应 `includeRawContent` |
| `concise_snippet` | 检索 | `false` | 对应 `conciseSnippet` |

三个开关必须为布尔值，不能将字符串 `"false"` 当作真值。
配置示例维护在仓库根目录 `config.example.yaml`；切换供应商时替换同名工具条目。

## 六种检索响应

必须按 `scope` 选择结果容器，不能假定所有响应都使用 `webpages`。

| `scope` | 响应列表 | 已观察到的差异 |
| --- | --- | --- |
| `webpage` | `webpages` | `link`、`snippet`、日期等 |
| `document` | `documents` | `link`、`snippet`、作者等 |
| `scholar` | `scholars` | `link`、`snippet`、日期等 |
| `image` | `images` | 可能仅有 `imageUrl`、`imageWidth`、`imageHeight`，缺少 `link` 和 `snippet` |
| `video` | `videos` | 作者、日期、`duration`、`coverImage` 等 |
| `podcast` | `podcasts` | 作者、日期、`duration`，摘要可能为空 |

成功检索返回 JSON 数组的字符串，使用 `indent=2, ensure_ascii=False`，不增加外层包装。
每项统一包含字符串字段 `title`、`url`、`snippet`，缺失或空值归一化为空字符串。
`url` 依次采用非空的 `url`、`link`、`imageUrl`。保留其余原始字段，包括 `link`、
图片尺寸、作者、时长及未来新增字段；不得仅提取三个公共字段而丢弃元数据。

响应顶层必须是对象，且包含当前 scope 对应的容器。容器为 `null` 或空列表时返回
`[]`；容器不是列表、非空列表完全没有对象项、顶层包含非空 `error` 或 `errCode`
时返回格式错误。混合列表跳过非对象项，保持有效对象的原始顺序。
缺失预期容器应视为格式错误，不能静默返回空列表。

## 获取、错误与资源生命周期

网页获取成功时原样返回 `response.text`，保留完整正文，不添加标题或截断到 4096 字符。
HTTP 成功但响应类型包含 `application/json` 时视为格式错误，避免把错误对象当作正文。

普通执行异常统一返回以 `Error: ` 开头的字符串，不返回 JSON 错误对象。
HTTP 状态错误保留状态码，超时返回明确提示；其他异常仅暴露异常类型。
`_MetasoError` 只能携带可公开的受控错误信息。日志只记录异常类型，不记录密钥、
请求内容、响应正文或原始异常消息。

每次调用创建独立的 `httpx.AsyncClient`，使用异步上下文管理器确保成功、失败和取消时
均关闭连接。取消必须继续传播，不能转换为普通错误字符串。

## 验证要求

修改后从仓库根目录使用 Python 3.12 或更新版本运行：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_metaso_tools.py' -v
```

离线测试使用真实工具入口和 HTTPX `MockTransport`，覆盖六种响应结构、元数据保留、
配置和凭据隔离、空及畸形响应、HTTP 与网络错误、连接关闭、取消传播、完整正文及工具参数。

修改 scope 映射或响应归一化时，逐个真实调用六种 scope，检查各自结果容器和字段，
不能仅凭网页检索通过就声称全部适配。使用小结果数控制验证请求量，禁止输出或保存密钥。
2026-10-23 已通过六种 scope 的逐项真实调用，以及用户提供的网易网页 reader 调用；
这只是当时的验证结果，不代表未来 API 响应结构不会变化。
