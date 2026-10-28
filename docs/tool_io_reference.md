# Tool 输入与输出参考

以 `DeerFlowClient.stream()` 的主 Agent 为入口，覆盖先前清单中的 **1–12、15–17，共 15 个工具**。按当前仓库实现整理；示例用于说明结构，不代表本次真实联网调用结果。

## 阅读约定

- `call` 只包含模型填写的参数，不包含注入的 `runtime`、`tool_call_id`。
- `result` 默认指 `ToolMessage.content`。JSON 示例表示这个**字符串解析后的内容**，不是说 Python 工具直接返回了字典或列表。
- `present_files`、`view_image`、`task`、`write_todos` 返回 `Command` 更新图状态；下文分别说明文本、状态及附加数据。`ask_clarification` 由中间件拦截。
- `<path>`、`<detail>`、`<N>` 等是动态值占位符；错误详情由文件系统、沙箱、服务端或依赖库决定，无法枚举每一条底层异常文案。
- 文件工具建议使用 `/mnt/user-data/workspace/`、`/mnt/user-data/uploads/`、`/mnt/user-data/outputs/` 下的绝对虚拟路径；技能及自定义挂载目录另受权限约束。不能把任意宿主机路径当作可访问路径。
- 每节列出工具自身的正常、空结果及异常分支；另见文末“中间件对输出的影响”。同一种业务失败不一定都有 `Error:` 前缀，也不应只依靠该前缀判断成功。

## 1. web_search

根据关键词检索网络资源。

源码：[deerflow/community/metaso/tools.py](../deerflow/community/metaso/tools.py)，`web_search_tool`、`_search_results`、`_error_result`。

**call**

```json
{"query": "人工智能", "scope": "webpage"}
```

| 参数 | 类型／默认值 | 说明及可选值 |
|---|---|---|
| `query` | string，必填 | 问题或关键词；工具自身不额外限制长度或拒绝空字符串，最终由服务端处理。 |
| `scope` | string，默认 `"webpage"` | 支持下表的 6 个值，区分大小写；虽然 schema 是 string，执行时仍检查支持范围。 |

| scope | 资源类型 | 上游响应读取的字段 |
|---|---|---|
| `webpage` | 网页 | `webpages` |
| `document` | 文档 | `documents` |
| `scholar` | 学术资源 | `scholars` |
| `image` | 图片 | `images` |
| `video` | 视频 | `videos` |
| `podcast` | 播客 | `podcasts` |

**result：所有 scope 最终统一成相同结构**

```json
[
  {"title": "示例标题", "url": "https://example.com/item", "snippet": "示例摘要"}
]
```

上游不同 scope 的原始字段可能不同，但当前封装**只保留三个字段**，没有透传作者、日期、图片尺寸等额外字段：

- `title`：上游 `title`，缺失或假值变为 `""`，其他值转成字符串。
- `url`：按 `url → link → imageUrl → ""` 依次选取第一个真值，再转成字符串。因此图片 scope 的链接也叫 `url`，不叫 `image_url`。
- `snippet`：上游 `snippet`，缺失或假值变为 `""`；不会用其他摘要字段补齐。

| 情况 | 实际输出 |
|---|---|
| 有结果 | 上述 JSON 数组字符串；即使字段全缺失，对应条目也是三个空字符串。 |
| 容器为 `[]` 或 `null` | `[]` |
| 数组中混有非对象 | 丢弃非对象，保留对象并归一化；若非空数组里一个对象也没有，则报格式错误。 |
| 不支持的 scope | `Error: Unsupported Metaso search scope` |
| 未配置密钥 | `Error: METASO_API_KEY is not configured` |
| 配置的结果数非法 | `Error: max_results must be a positive integer` |
| 配置的布尔选项非法 | `Error: <include_summary/include_raw_content/concise_snippet> must be a boolean` |
| 响应不是对象、带真值 `error` / `errCode`、缺少对应容器、容器类型错误 | `Error: Metaso returned an unexpected response format` |
| HTTP 非成功状态 | `Error: Metaso request failed (HTTP <状态码>)` |
| 网络超时 | `Error: Metaso request timed out` |
| JSON 解码失败、连接错误等其他异常 | `Error: Metaso request failed (<异常类名>)` |

注意：`max_results`、`include_summary`、`include_raw_content`、`concise_snippet`、`timeout` 属于配置项，不是本工具的 call 参数；即使请求了上游额外内容，当前归一化也不会额外增加输出字段。

## 2. web_fetch

获取网页正文。

源码：[deerflow/community/metaso/tools.py](../deerflow/community/metaso/tools.py)，`web_fetch_tool`。

**call**

```json
{"url": "https://example.com/article"}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `url` | string，必填 | 带协议的完整 URL；使用用户提供或搜索结果中的链接，不自行添加 `www`。不能依靠本工具读取需要登录的私有页面。 |

**result**

```text
网页正文原文，可包含 Markdown、换行和标题。
```

| 情况 | 实际输出 |
|---|---|
| 成功 | `response.text`，无固定 JSON 包装；服务返回空正文时就是空字符串。 |
| 成功状态但 Content-Type 包含 `application/json` | `Error: Metaso returned an unexpected response format`，即使正文看起来可用也拒绝。 |
| 未配置密钥 | `Error: METASO_API_KEY is not configured` |
| HTTP 错误 | `Error: Metaso request failed (HTTP <状态码>)` |
| 超时 | `Error: Metaso request timed out` |
| 其他异常 | `Error: Metaso request failed (<异常类名>)` |

非法 URL 不一定在本地校验失败；其输出取决于上游返回或请求异常。成功响应里的非 JSON 错误文字也可能直接作为正文返回。

## 3. image_search

通过 DuckDuckGo 搜索参考图片；与 `web_search(scope="image")` 的输出结构不同。

源码：[deerflow/community/image_search/tools.py](../deerflow/community/image_search/tools.py)，`image_search_tool`；结果数归一化见 [search_max_results.py](../deerflow/community/search_max_results.py)。

**call**

```json
{"query": "森林", "max_results": 5, "size": "Large", "color": null, "type_image": "photo", "layout": "Wide", "license_image": null}
```

| 参数 | 类型／默认值 | 说明及可选值 |
|---|---|---|
| `query` | string，必填 | 图片关键词。 |
| `max_results` | integer，默认 `5` | 期望数量；配置中的同名值优先覆盖 call。归一化后非正数／非法配置回退 5，工具未设置统一上限；不保证实际返回这么多。 |
| `size` | string/null，默认 null | `Small`、`Medium`、`Large`、`Wallpaper`。 |
| `color` | string/null，默认 null | `color`、`Monochrome`、`Red`、`Orange`、`Yellow`、`Green`、`Blue`、`Purple`、`Pink`、`Brown`、`Black`、`Gray`、`Teal`、`White`。`color` 表示彩色。 |
| `type_image` | string/null，默认 null | `photo`、`clipart`、`gif`、`transparent`、`line`。 |
| `layout` | string/null，默认 null | `Square`、`Tall`、`Wide`。 |
| `license_image` | string/null，默认 null | `any`、`Public`、`Share`、`ShareCommercially`、`Modify`、`ModifyCommercially`；是检索筛选条件，不能代替核实原图授权。 |

这些筛选值由工具说明约定，schema 并非枚举，工具也没有逐一校验；非空值转交服务端。`region`、`safesearch` 不暴露为 call 参数，内部默认 `wt-wt`、`moderate`。

**result：正常**

```json
{
  "query": "森林",
  "total_results": 1,
  "results": [
    {"title": "森林照片", "image_url": "https://example.com/a.jpg", "thumbnail_url": "https://example.com/thumb.jpg", "source_url": "https://example.com/page"}
  ],
  "usage_hint": "Use the 'image_url' values as reference images in image generation. Download them first if needed."
}
```

`total_results` 是安全过滤后的条数。标题缺失或非字符串变为 `""`；URL 经安全检查，不安全或缺失时为 `""`。`image_url` 和 `thumbnail_url` 至少一个有效才保留该条；`source_url` 可以为空，未透传上游额外字段。

**result：空结果／异常**

```text
Error: No images found
```

```text
Error: No safe image URLs found
```

- 第一种既可能是确实无结果，也可能是缺少 `ddgs` 依赖或检索调用失败；这些情况统一返回 `Error: No images found`，不再返回含 error/query 的 JSON 对象。
- 第二种表示原始结果非空，但所有条目都不是对象或没有安全图片／缩略图 URL。
- 配置加载、初始化等没有被内部检索异常处理捕获的异常，会走文末以 `Error:` 开头的通用异常包装。成功结果仍使用上述 JSON 结构。

## 4. ls

列出目录，工具说明为最多两层的树形文本，具体样式由沙箱实现决定。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`ls_tool`。

**call**

```json
{"path": "/mnt/user-data/workspace", "description": "查看工作目录"}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `path` | string，必填 | 要列出的目录绝对路径。 |
| `description` | string，默认 `""` | 展示用简短说明，不参与目录检索。 |

**result**

```text
/mnt/user-data/workspace/a.txt
/mnt/user-data/workspace/reports/
```

上述仅为示意，返回的是多行字符串，不是固定 JSON 数组。

| 情况 | 实际输出 |
|---|---|
| 正常目录 | 沙箱返回的目录项以换行拼接；本地宿主路径会映射为虚拟路径。 |
| 空目录／过滤禁用技能后无可见项 | `(empty)` |
| 内容过长 | 保留开头并追加 `... [truncated: showing first <保留数> of <总数> chars. Use a more specific path to see fewer results] ...`；极小预算可能只剩截取文本。 |
| 目录不存在 | `Error: Directory not found: <path>` |
| 权限不足 | `Error: Permission denied: <path>` |
| 禁用技能目录 | `Error: Skill '<name>' is disabled. Access to its files is blocked. Enable the skill in settings before using it.` |
| 沙箱异常 | `Error: <沙箱异常说明>` |
| 其他异常，包括未被独立捕获的路径类型错误 | `Error: Unexpected error listing directory: <异常类名>: <detail>` |

当前配置 `ls_output_max_chars=20000`，配置为 0 可关闭该层截断。

## 5. read_file

读取 UTF-8 文本，可按行读取。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`read_file_tool`、`_truncate_read_file_output`；固定提示见 [read_file_contract.py](../deerflow/sandbox/read_file_contract.py)。

**call**

```json
{"path": "/mnt/user-data/workspace/a.txt", "description": "读取文本", "start_line": 1, "end_line": 20}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `path` | string，必填 | 文件绝对路径。 |
| `description` | string，默认 `""` | 展示说明。 |
| `start_line` | integer/null，默认 null | 从 1 开始，包含该行；省略从第一行开始。非 null 时必须 ≥1。 |
| `end_line` | integer/null，默认 null | 包含该行；省略读到末尾。必须 ≥1 且不小于有效起始行。超过末尾时按实际内容读取。 |

**result**

| 情况 | 实际输出 |
|---|---|
| 正常 | 文件全文或指定行的原文字符串，不自动增加行号；按行读取的换行形式由读取实现处理。 |
| 空文件／读到的空行无内容 | `(empty)` |
| `start_line > 1` 且超过文件长度 | `(start_line exceeds file length)` |
| 起始行小于 1 | `(start_line must be >= 1)` |
| 结束行小于 1 | `(end_line must be >= 1)` |
| 起始行大于结束行 | `(invalid line range: start_line must be <= end_line)` |
| 文件不存在 | `Error: File not found: <path>` |
| 权限不足 | `Error: Permission denied reading file: <path>` |
| 路径是目录 | `Error: Path is a directory, not a file: <path>` |
| UTF-8 解码失败 | `Error: cannot read '<path>' as text — it appears to be a binary file (e.g. .xlsx, .pdf, or an image). read_file only supports UTF-8 text. Use bash with a suitable library instead (pandas/openpyxl for spreadsheets), or view_image for images.` |
| 禁用技能 | 与 `ls` 相同的禁用技能提示。 |
| 沙箱异常 | `Error: <沙箱异常说明>` |
| 其他异常 | `Error: Unexpected error reading file: <异常类名>: <detail>` |

当前配置 `read_file_output_max_chars=50000`。超限保留开头，并追加以 `... [truncated:` 开头的提示：

- 在行边界截断：给出已展示的行数、字符数和继续读取的 `start_line`。
- 在超长行内部截断：注明被截断行及行内剩余信息，不能认为这行已完整读完。
- 预算极小时：只给缩小读取范围的提示，该提示自身也可能被截短。

截断提示的实际格式举例（数字是占位示例）：

```text
... [truncated: showing first 20 of 100 lines (500 of 3000 chars). Continue with start_line=21, or use start_line/end_line to read a specific range] ...
```

若切在行内，会出现 `cut inside line <行号>`，并根据该行能否单独读完，提示重新读取该行、单独指定 start_line/end_line，或改用 `bash` 的 `cut -c`。指定读取范围时，提示里的行号仍是原文件行号。

因此，`(empty)`、行范围错误和截断后的部分文本都不能一概当成“完整文件已读成功”。

## 6. glob

按文件名模式搜索路径。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`glob_tool`、`_format_glob_results`；本地匹配见 [search.py](../deerflow/sandbox/search.py)。

**call**

```json
{"pattern": "**/*.py", "path": "/mnt/user-data/workspace", "description": "查找 Python 文件", "include_dirs": false, "max_results": 200}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `pattern` | string，必填 | 相对搜索根的 glob，支持 `*`、`?`、字符集合、`**`。本地实现中无 `/` 的模式按各层文件名匹配，带 `/` 的模式相对根目录匹配，`**` 可匹配零层或多层。 |
| `path` | string，必填 | 搜索根目录绝对路径，必须是目录。 |
| `description` | string，默认 `""` | 展示说明。 |
| `include_dirs` | boolean，默认 false | true 时也返回匹配目录。 |
| `max_results` | integer，默认 `200` | ≤0 回退默认值；工具上限 1000。有效数量还取配置限制与请求限制的较小值；当前配置 200。 |

**result：正常**

```text
Found 2 paths under /mnt/user-data/workspace
1. /mnt/user-data/workspace/a.py
2. /mnt/user-data/workspace/src/b.py
```

| 情况 | 实际输出 |
|---|---|
| 无匹配 | `No files matched under <path>` |
| 达到上限且有可见结果 | 首行增加 `(showing first <N>)`，末行增加 `Results truncated. Narrow the path or pattern to see fewer matches.` |
| 搜索达到上限，但权限／禁用技能过滤后无可见结果 | `Search under <path> stopped at its result limit with no files matched in the part it covered; results are incomplete. Narrow the path or pattern.` |
| 根目录不存在 | `Error: Directory not found: <path>` |
| 路径不是目录 | `Error: Path is not a directory: <path>` |
| 权限不足 | `Error: Permission denied: <path>` |
| 禁用技能／沙箱异常 | 与 `ls` 相同的禁用技能提示／`Error: <沙箱异常说明>`。 |
| 其他异常 | `Error: Unexpected error searching paths: <异常类名>: <detail>` |

达到数量上限即可能标记截断，不代表一定存在更多结果。搜索会忽略实现中规定的目录／文件，并过滤禁用技能，不等于对整棵文件系统无遗漏扫描。

## 7. grep

搜索文件中的匹配行。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`grep_tool`、`_format_grep_results`。

**call**

```json
{"pattern": "TODO", "path": "/mnt/user-data/workspace", "description": "查找待办", "glob": "**/*.py", "literal": true, "case_sensitive": false, "max_results": 100}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `pattern` | string，必填 | 默认正则表达式；`literal=true` 时按普通字符串处理。 |
| `path` | string，必填 | 文件或搜索根目录的绝对路径。 |
| `description` | string，默认 `""` | 展示说明。 |
| `glob` | string/null，默认 null | 候选文件名过滤，如 `**/*.py`；不是内容正则。 |
| `literal` | boolean，默认 false | true 时正则元字符无特殊含义。 |
| `case_sensitive` | boolean，默认 false | 默认不区分大小写，true 时区分。 |
| `max_results` | integer，默认 `100` | 限制匹配行数，不是文件数；≤0 回退默认值，工具上限 500，有效值还受配置限制；当前配置 100。 |

**result：正常**

```text
Found 1 matches under /mnt/user-data/workspace
/mnt/user-data/workspace/a.py:12: # TODO: 补充校验
```

行号从 1 开始。每行命中一次就输出一条，不是把行中每一个匹配片段分别计数。

| 情况 | 实际输出 |
|---|---|
| 无匹配 | `No matches found under <path>` |
| 有结果但截断 | 首行增加 `(showing first <N>)`，末行 `Results truncated. Narrow the path or add a glob filter.` |
| 截断后无可见匹配 | `Search under <path> stopped at its result limit with no matches in the part it covered; results are incomplete. Narrow the path or add a glob filter.` |
| 正则非法 | `Error: Invalid regex pattern: <detail>` |
| 路径不存在 | `Error: Directory not found: <path>`；即使传入的是文件，当前该分支也使用这句文案。 |
| 路径类型错误 | `Error: Path is not a directory: <path>` |
| 权限不足 | `Error: Permission denied: <path>` |
| 禁用技能／沙箱异常 | 与 `ls` 相同的禁用技能提示／`Error: <沙箱异常说明>`。 |
| 其他异常 | `Error: Unexpected error searching file contents: <异常类名>: <detail>` |

本地搜索会跳过忽略项、二进制文件、大于 1000000 字节的文件、遍历时的符号链接及读取异常文件；超过 2000 字符的行也会跳过。命中行展示最多 200 字符，较长时保留前 197 字符并加 `...`。因此“无匹配”不保证每个文件都被全文检查，“找到的行”也不保证是完整行文本。

## 8. write_file

创建、覆盖或追加文本文件。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`write_file_tool`、`_format_write_file_error`；读后写保护见 [read_before_write_middleware.py](../deerflow/agents/middlewares/read_before_write_middleware.py)。

**call**

```json
{"path": "/mnt/user-data/outputs/report.md", "content": "# 报告\n", "description": "写入报告", "append": false}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `path` | string，必填 | 目标文件绝对路径。 |
| `content` | string，必填 | 要写入的文字；空字符串也是有效内容，覆盖时会清空文件。 |
| `description` | string，默认 `""` | 展示说明。 |
| `append` | boolean，默认 false | false 覆盖；true 追加。已存在文件在两种模式下都受读后写保护。 |

**result**

| 情况 | 实际输出 |
|---|---|
| 创建／覆盖／追加成功 | `OK`，没有文件路径或字节数 JSON。 |
| 非追加调用超过大小限制 | 以 `Error: write_file content (<实际字节数> bytes) exceeds the <上限>-byte single-call limit.` 开头，后接下方固定拆分建议。 |
| 写入目录 | `Error: Path is a directory, not a file: <path>` |
| 权限不足 | `Error: Permission denied writing to file: <path>` |
| 沙箱／OS／其他写入异常 | `Error: Failed to write file '<path>': <异常类名>: <detail>` |
| 已存在文件未读取当前版本 | 中间件拒绝，见文末读后写保护；工具本体尚未执行。 |

非追加内容默认上限为 **80×1024 字节（UTF-8）**；`DEERFLOW_WRITE_FILE_MAX_BYTES` 可覆盖，≤0 关闭限制；追加不受这个工具层限制。错误详情也可能被中间截断，并带 `... [write_file error truncated: <N> chars skipped] ...` 标记。内容是否完整还受模型输出限制影响，不能把 80 KiB 当成每次都能生成的保证。

超限错误后接的完整建议：

```text
Split the content into smaller pieces: either (a) write the first section now, then use `str_replace` for further edits, or (b) call write_file again with append=True carrying the next section. See SIZE POLICY in the tool docstring or issue #3189 for the rationale.
```

## 9. str_replace

替换已有文件中的文本。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`str_replace_tool`、`_match_line_endings`。

**call**

```json
{"path": "/mnt/user-data/workspace/a.txt", "old_str": "旧内容", "new_str": "新内容", "description": "更新内容", "replace_all": false}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `path` | string，必填 | 目标文件绝对路径。 |
| `old_str` | string，必填 | 精确匹配文本，不是正则。空字符串当前实现为不修改文件并返回 `OK`。 |
| `new_str` | string，必填 | 替换文本，`""` 表示删除匹配内容。 |
| `description` | string，默认 `""` | 展示说明。 |
| `replace_all` | boolean，默认 false | false 仅替换第一次；true 替换所有匹配。 |

**result**

| 情况 | 实际输出 |
|---|---|
| 替换成功 | `OK` |
| `old_str=""` | 在成功读取文件后返回 `OK`，不写入；之前仍可能被读后写保护拒绝。 |
| 文件为空或找不到 `old_str` | `Error: String to replace not found in file: <path>` |
| 文件不存在 | `Error: File not found: <path>` |
| 权限不足 | `Error: Permission denied accessing file: <path>` |
| 沙箱异常 | `Error: <沙箱异常说明>` |
| 其他异常，如目录／解码错误 | `Error: Unexpected error replacing string: <异常类名>: <detail>` |
| 没读过文件当前版本 | 文末读后写保护错误。 |

**实现与说明有一处差异**：docstring 要求默认模式下 `old_str` 恰好出现一次，但工具本体没有检查多次匹配，实际只替换第一次，不会专门返回“匹配不唯一”错误。CRLF 文件会在适用时调整新旧字符串的换行，避免因模型给出 LF 而无法匹配。

## 10. bash

执行命令，返回文本；输出细节由沙箱实现决定。

源码：[deerflow/sandbox/tools.py](../deerflow/sandbox/tools.py)，`bash_tool`；当前本地实现见 [local_sandbox.py](../deerflow/sandbox/local/local_sandbox.py)，`execute_command`。

**call**

```json
{"command": "pwd", "description": "查看当前目录"}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `command` | string，必填 | Shell 命令。默认工作目录是线程 workspace，文件操作优先用允许的绝对虚拟路径；本地执行会校验并转换路径。 |
| `description` | string，默认 `""` | 展示说明。 |

没有模型可填的 `timeout`、`cwd` 或 `env` 参数，超时及环境由配置和运行时控制。长期服务应后台运行并重定向日志。

**result：当前 LocalSandboxProvider**

| 情况 | 实际输出 |
|---|---|
| 成功且有 stdout | stdout 原文；不会固定附带 `Exit Code: 0`。 |
| 成功且无 stdout/stderr | `(no output)`；这是当前本地 provider 的明确占位文本。 |
| 只有 stderr | stderr 原文；即使命令退出码为 0，也可能有警告文本。 |
| 同时有 stdout/stderr | `<stdout>\nStd Error:\n<stderr>` |
| 非零退出 | 上述输出后追加 `\nExit Code: <退出码>`；不是固定 `Error:` 前缀。 |
| 执行超时 | 保留已捕获输出并追加超时提示及 `Exit Code: 124`；超时提示由沙箱生成。 |
| 本地 Bash 未获配置允许 | `Error: Host bash execution is disabled for LocalSandboxProvider because it is not a secure sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or set sandbox.allow_host_bash: true only in a fully trusted local environment.` |
| 路径权限／沙箱异常 | `Error: <detail>` |
| 其他异常 | `Error: Unexpected error executing command: <异常类名>: <detail>` |

当前配置输出上限 20000 字符，超出后保留头尾并插入截断标记，保留尾部退出码证据；0 关闭该层截断，1–31 的配置采用至少 32 字符的有效预算。注入凭据会脱敏，本地路径会映射为虚拟路径。远程沙箱的命令输出格式可能不同，不能假定所有 provider 都与本地实现完全一致。

本地超时的完整提示示例（60 为实际超时秒数）：

```text
Command timed out after 60 seconds and was terminated. To run a long-lived process such as a web server, start it in the background and redirect its output, e.g. `your-command > /mnt/user-data/workspace/server.log 2>&1 &`.
Exit Code: 124
```

工具层头尾截断标记为 `... [middle truncated: <N> chars skipped] ...`；沙箱自身的捕获预算也可能先截断输出。

## 11. present_files

把产出文件路径登记到当前线程，让客户端展示或下载。

源码：[deerflow/tools/builtins/present_file_tool.py](../deerflow/tools/builtins/present_file_tool.py)，`present_file_tool`。

**call**

```json
{"filepaths": ["/mnt/user-data/outputs/report.md"]}
```

| 参数 | 类型／默认值 | 说明 |
|---|---|---|
| `filepaths` | string[]，必填 | outputs 内文件的绝对虚拟路径，也接受当前线程 outputs 对应的宿主绝对路径并归一化；可传多个，`[]` 也是有效列表。 |

**result：正常**

```text
Successfully presented files
```

同时 `Command.update.artifacts` 更新为归一化路径列表：

```json
["/mnt/user-data/outputs/report.md"]
```

| 情况 | 实际输出与行为 |
|---|---|
| 空列表 | 同样返回成功文本，提交空 artifacts 更新。 |
| 多个路径中任意一个非法 | 整次返回错误，不登记其中其他路径。 |
| 路径不在当前 outputs 内 | `Error: Only files in /mnt/user-data/outputs can be presented: <path>` |
| 缺少 runtime state | `Error: Thread runtime state is not available` |
| 缺少线程 ID | `Error: Thread ID is not available in runtime context or runtime config` |
| 缺少 outputs 状态路径 | `Error: Thread outputs path is not available in runtime state` |
| 其他 ValueError | `Error: <detail>` |
| 其他未捕获异常 | 文末通用异常包装。 |

工具本体只校验路径归属，**不校验文件是否存在、是否是普通文件，也不读取文件内容**；成功登记不等于客户端下载必定成功。artifacts 由状态 reducer 合并去重，不是覆盖整个历史列表。

## 12. ask_clarification

向用户提出澄清问题；标准中间件会记录问题并结束当前图执行，等待后续用户输入。

源码：[clarification_tool.py](../deerflow/tools/builtins/clarification_tool.py)、[clarification_middleware.py](../deerflow/agents/middlewares/clarification_middleware.py)。

**call**

```json
{"question": "需要哪种格式？", "clarification_type": "approach_choice", "context": null, "options": ["PDF", "Word"], "fields": null}
```

| 参数 | 类型／默认值 | 说明及可选值 |
|---|---|---|
| `question` | string，必填 | 要展示的问题。 |
| `clarification_type` | enum string，必填 | `missing_info` 缺信息、`ambiguous_requirement` 需求有歧义、`approach_choice` 方案选择、`risk_confirmation` 风险确认、`suggestion` 建议。 |
| `context` | string/null，默认 null | 解释提问背景；非空时显示在问题前。 |
| `options` | string[]/null，默认 null | 单选列表；去标签、去空白及去重后使用，可搭配自由输入。 |
| `fields` | object[]/null，默认 null | 表单字段；有效且非空时优先于 options。 |

`fields` 中每个对象：

| 字段 | 类型／默认值 | 说明 |
|---|---|---|
| `name` | string，必填 | 唯一非空标识，不使用 `constructor`、`toString` 等原型保留名。 |
| `label` | string，默认 name | 展示名称。 |
| `type` | enum string，默认 `text` | `text`、`textarea`、`number`、`select`、`multi_select`、`checkbox`、`date`。 |
| `required` | boolean，默认 false | 是否必填；checkbox 设 true 表示必须勾选同意。 |
| `options` | string[] | select / multi_select 要提供有效非空选项；其他类型忽略。 |
| `placeholder` | string，可省略 | 输入提示。 |

最多 16 个字段、每字段 24 个选项；name/label/option/placeholder 每项最多 200 字符；归一化 fields JSON 最多 16384 UTF-8 字节。

**result：正常单选**

```text
🔀 需要哪种格式？

  1. PDF
  2. Word
```

不同类型依次对应 `❓`、`🤔`、`🔀`、`⚠️`、`💡` 图标。有 context 时先展示图标和背景，再展示问题；表单模式展示编号字段及选项，不展示顶层单选列表，末尾追加 `Please reply with a value for each field.`。

除文本外，`ToolMessage.artifact.human_input` 携带结构化请求，例如：

```json
{
  "version": 1,
  "kind": "human_input_request",
  "source": "ask_clarification",
  "request_id": "clarification:<tool_call_id>",
  "tool_call_id": "<tool_call_id>",
  "clarification_type": "approach_choice",
  "question": "需要哪种格式？",
  "context": null,
  "input_mode": "choice_with_other",
  "options": [
    {"id": "option-1", "label": "PDF", "value": "PDF"},
    {"id": "option-2", "label": "Word", "value": "Word"}
  ]
}
```

| 情况 | 实际输出与行为 |
|---|---|
| 无有效 options / fields | `version=1`、`input_mode=free_text`，不带 options / fields。 |
| 有有效 options | 上述 `choice_with_other` 结构。context 只有 call 显式提供时才带。 |
| 有有效 fields | `version=2`、`input_mode=form`，携带归一化 fields；字段内选项变成 `{id,label,value}` 对象列表，不是原始字符串列表。 |
| 表单条目非法、字段名重复／保留、超数量／长度／总大小 | 整张表单失效，回退有效的顶层 options 或纯文本问题；不固定返回 `Error:`。 |
| 字段 type 未知／select 没有有效选项 | 该字段降级为 `text`。这是中间件防御行为，不是建议的输入值。 |
| 同轮还有其他工具调用 | 正常交互模式下丢弃其他调用，防止用户回答前产生操作。 |
| 运行策略禁止澄清 | 返回以 `Clarification is disabled in this context — the human is not present to answer synchronously.` 开头的 ToolMessage，继续说明可逆工作采用合理假设、缺授权的高风险工作停止；不创建询问卡片，也不跳到 END。 |
| 绕开中间件直接调用占位函数 | `Clarification request processed by middleware`；不是标准 stream 路径的实际结果。 |

标准路径使用 `Command(goto=END)` 结束当前轮，不是普通“工具返回后再调用一次模型”的流程，也不是 LangGraph 的 `interrupt()` 暂停对象。

## 15. view_image

读取图片，并在后续模型请求中注入图片内容。

源码：[view_image_tool.py](../deerflow/tools/builtins/view_image_tool.py)、[view_image_middleware.py](../deerflow/agents/middlewares/view_image_middleware.py)。

**call**

```json
{"image_path": "/mnt/user-data/uploads/photo.png"}
```

| 参数 | 类型／默认值 | 说明及可选值 |
|---|---|---|
| `image_path` | string，必填 | 只接受 workspace/uploads/outputs 下的绝对虚拟路径；扩展名支持 `.jpg`、`.jpeg`、`.png`、`.webp`、`.gif`，扩展名转小写后检查。每次一张，不接受 URL、base64 或任意宿主路径。 |

**result：正常**

```text
Successfully read image
```

同时更新 `viewed_images[image_path]`，包括 `mime_type`、`size`、`actual_path`、`sha256`；按运行情况还可能有 `source_sandbox_id`、`blob_ref`。这些是内部状态，不是模型填写的参数或统一返回 JSON。

`ViewImageMiddleware` 在后续模型输入中追加图片上下文 HumanMessage，包含说明文本和如下内容块，而不是把图片字节直接塞进成功 ToolMessage：

```json
{"type": "image_url", "image_url": {"url": "data:image/png;base64,<图片数据>"}}
```

**result：异常及后续差异**

| 情况 | ToolMessage.content |
|---|---|
| 路径不在允许目录 | `Error: Only image paths under /mnt/user-data/workspace, /mnt/user-data/uploads, /mnt/user-data/outputs are allowed` |
| 路径越界／运行状态或权限问题 | `Error: <detail>` |
| 不支持的扩展名 | `Error: Unsupported image format: <suffix>. Supported formats: .jpg, .jpeg, .png, .webp, .gif` |
| 文件不存在 | `Error: Image file not found: <image_path>` |
| 路径不是文件 | `Error: Path is not a file: <image_path>` |
| 文件大于 20 MiB | `Error: Image file is too large: <实际字节数> bytes. Maximum supported size is 20971520 bytes`；远程下载层也可能提前以读取错误拒绝。 |
| 元数据读取失败 | `Error reading image metadata: <异常类名>: <detail>` |
| 文件读取／下载失败 | `Error reading image file: <异常类名>: <detail>` |
| 读取期间长度变化 | `Error: Image file changed during read` |
| 文件头不是支持的图片格式 | `Error: File contents do not match a supported image format` |
| 文件头和扩展名不一致 | `Error: Image contents are <实际 MIME>, but file extension indicates <扩展名对应 MIME>` |
| 启用共享存储，但图片持久化失败 | `Error: Failed to persist image in shared blob storage` |

后续模型注入还受图片数量／字节预算和文件是否仍可用影响：可以出现 `Omitted viewed images: <N>. Call view_image again to inspect a specific image.`，或 `(file unavailable or changed: <path>)`，因此成功读取后也不能保证每次后续请求都携带所有历史图片。文件头校验不是完整图像解码验证；损坏图片仍可能在模型或下游处理时失败。

## 16. task

把任务委派给子智能体，等待其终态后返回结果。

源码：[task_tool.py](../deerflow/tools/builtins/task_tool.py)，`task_tool`、`_task_result_command`；输出格式及枚举见 [status_contract.py](../deerflow/subagents/status_contract.py)。

**call**

```json
{"prompt": "检查代码并汇报问题", "subagent_type": "general-purpose", "acceptance_criteria": null, "description": "检查代码", "context_mode": "isolated"}
```

| 参数 | 类型／默认值 | 说明及可选值 |
|---|---|---|
| `prompt` | string，必填 | 完整、明确的委派任务。 |
| `subagent_type` | string，必填 | 内置 `general-purpose`、`bash`；也可为已注册的自定义类型。实际可用列表受配置、允许列表及 Bash 策略限制，不是任意字符串都有效。 |
| `acceptance_criteria` | string[]/null，默认 null | 可选验收条件列表；见下方。 |
| `description` | string，默认 `""` | 日志／展示用简短描述，工具建议 3–5 个词；不是独立的任务内容。 |
| `context_mode` | enum string，默认 `"isolated"` | `isolated` 只带委派输入；`snapshot` 额外带派发时父会话保留历史及摘要。之后的父会话不持续同步。 |

验收条件的可识别形式：`file:<path> exists`、`file:<path> non-empty`、`file_written:<path>`、`tests_passed:<command>`、`file:<path> json-valid`、`file:<path> csv-columns:["id","name"]`。任意其他文字也可输入，但可能只能标为 UNVERIFIED。JSON 校验预算 50000 字节且只校验语法；CSV 只校验首条表头，允许额外列，表头读取预算 50000 字节，不校验数据行。

**result：正常与各终态**

| `subagent_status` | ToolMessage.content |
|---|---|
| `completed` | `Task Succeeded. Result: <子智能体报告>` |
| `completed` 且触发预算／循环限制，但有可用输出 | `Task Succeeded (capped: <原因>). Result: <报告或部分结果>` |
| `failed`，有错误详情 | `Task failed. Error: <detail>` |
| `failed`，无详情 | `Task failed.` |
| `failed` 且触发限制 | `Task failed (capped: <原因>). Error: <detail>`；无详情时为 `Task failed (capped: <原因>).` |
| `cancelled` | `Task cancelled by user.`；有自定义详情时为 `Task cancelled by user. Error: <detail>` |
| `timed_out` | `Task timed out.`；有详情时为 `Task timed out. Error: <detail>` |
| `polling_timed_out` | 直接返回轮询错误详情，例如 `Task polling timed out after <分钟数> minutes. This may indicate the background task is stuck. Status: <状态>`；无详情时为 `Task polling timed out.` |

限制原因的文本是 `token budget`、`turn budget`、`repeated tool-call loop`，对应结构化 stop reason `token_capped`、`turn_capped`、`loop_capped`。

| 异常触发条件 | 对应输出 |
|---|---|
| 未知 context_mode | failed，详情 `Unknown context_mode '<value>'. Use isolated or snapshot.` |
| 未知／不允许的 subagent_type | failed，详情 `Unknown subagent type '<value>'. Available: <可用类型>` |
| Bash 子智能体被策略禁止 | failed，详情为本地 Bash 子智能体禁用说明。 |
| 后台任务记录丢失 | failed，详情 `Task <工具调用ID> disappeared from background tasks`。 |
| 子智能体执行失败／取消／超时 | 上述对应终态格式，具体详情由执行器提供。 |
| 创建、派发、轮询等抛出未捕获异常 | 通用异常中间件改写为 failed 格式并补充恢复建议；不是普通 `Error: Tool ...` 文本。 |
| 父调用被取消 | 传播取消并清理／请求取消子任务；不保证还能产出一条正常 ToolMessage。 |

有验收结果时，报告后追加文本清单，逐条为 `[holds]`、`[does not hold]` 或 `[UNVERIFIED]`；验收检查自身异常时可能没有清单，不能把缺失当通过。

`Command.update.messages` 中还带有 `ToolMessage.additional_kwargs`：

| 字段 | 出现条件及内容 |
|---|---|
| `subagent_status` | 必有，以上 5 种终态。 |
| `subagent_stop_reason` | 触发限制时出现，以上 3 种枚举。 |
| `subagent_error` | 非完成态且存在非空错误；有界文本。 |
| `subagent_result_brief`、`subagent_result_sha256` | completed 且报告非空时出现；摘要最多 2000 字符，SHA-256 对应完整报告。 |
| `subagent_model_name` | 已知实际模型时出现。 |
| `subagent_token_usage` | 有用量时出现，含 input_tokens / output_tokens / total_tokens。 |
| `subagent_tool_receipts` | 有执行凭据时出现。 |
| `subagent_receipt_verdict` | completed 且有有效引用检查结果时出现。 |
| `subagent_acceptance_verdict` | completed 且有有效验收结果时出现；与文本清单对应。 |

`artifact` 按情况携带引用来源、知识检索来源，或为空；不是固定报告 JSON。执行过程中还可能产生子任务进度事件，不能把它们当最终工具返回。`completed` 只代表执行完成，验收仍可能失败或未验证。

## 17. write_todos

创建或更新当前计划，**每次替换完整列表**。

项目入口：[todo_middleware.py](../deerflow/agents/middlewares/todo_middleware.py)，`TodoMiddleware` 继承 LangChain `TodoListMiddleware`。工具参数与基础返回参照 [LangChain 官方实现](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/todo.py)；项目要求 `langchain>=1.3`，该链接不是本地已安装版本的锁定快照。

**call**

```json
{"todos": [{"content": "检查代码", "status": "in_progress"}, {"content": "汇报结果", "status": "pending"}]}
```

| 参数 | 类型／默认值 | 说明及可选值 |
|---|---|---|
| `todos` | object[]，必填 | 完整的新列表；`[]` 清空列表，不是增量追加。 |
| `todos[].content` | string，必填 | 任务描述。 |
| `todos[].status` | enum string，必填 | `pending` 未开始、`in_progress` 执行中、`completed` 已完成。 |

**result**

```text
Updated todo list to [{'content': '检查代码', 'status': 'in_progress'}, {'content': '汇报结果', 'status': 'pending'}]
```

这是包含 Python 列表表示的字符串，不是 JSON；同时 `Command.update.todos` 存储结构化列表。

| 情况 | 实际输出与行为 |
|---|---|
| 空列表 | `Updated todo list to []`，状态列表清空。 |
| 同一模型轮并行调用多次 | 中间件为每次调用返回下方错误文本，不正常更新计划。 |
| 缺字段、状态非法、参数类型不符 | schema 校验错误，走依赖库 ToolNode 或通用异常处理；错误细节依赖安装版本。 |
| 工具被授权策略移除 | 不能正常调用；项目 TodoMiddleware 同时关闭计划提示及未完成提醒。 |
| 列表仍有未完成项，但模型直接结束 | 项目中间件可注入提醒并再次调用模型，提醒次数受限；这不是 `write_todos` 自身的另一种返回。 |

并行重复调用的错误文本：

```text
Error: The `write_todos` tool should never be called multiple times in parallel. Please call it only once per model invocation to update the todo list.
```

## 中间件对输出的影响

上面是各工具的输出分支。通过 `stream()` 运行时还应区分以下情况：

| 情况 | 对模型／调用方的影响 |
|---|---|
| 输入缺字段、类型错误、枚举值错误 | 可能在工具本体执行前被 schema 拒绝。ToolNode 版本决定其校验错误格式；若异常到达 ToolErrorHandlingMiddleware，则使用下方通用包装，不能保证命中工具内部错误文案。 |
| 权限、技能工具策略、保护规则拒绝 | 可在执行前返回策略错误 ToolMessage，或工具根本不暴露；这不代表底层 API／文件操作已尝试。 |
| 已存在文件未读当前版本便写入 | `Error: <tool_name> blocked — <path> already exists (<行数说明>) and you have not read its current version. Any write invalidates earlier reads, so re-read before every modification. <读取建议>`。适用于 write_file（含 append）及 str_replace；每次成功修改后要重新读取。 |
| 工具抛出未捕获异常 | 一般为 `Error: Tool '<tool_name>' failed with <异常类名>: <detail>. Continue with available context, or choose an alternative tool.`；detail 先处理并限长，消息 status 为 error。task 使用其专有 failed 格式。 |
| 图控制流异常／取消 | 部分会继续传播，不转换为正常错误结果，所以不是每次调用都会有完整 result。 |
| 超大结果 | ToolOutputBudgetMiddleware 可把全文写入文件，替换为结构摘要和文件引用；无法持久化时使用有界截断。此时即使工具原来返回 JSON，模型看到的也可能是摘要文本。 |
| 脱敏／内容清理／路径映射 | 可能替换凭据、控制字符、不可信标记、宿主路径；不能保证最终内容与底层原始输出逐字一致。 |
| 工具凭据／来源／进度元数据 | 可以额外出现在 ToolMessage 的 additional_kwargs / artifact 或流事件里，不应当作每个工具本体都有的业务返回字段。 |

外置大结果的文本开头形如：

```text
[Full <tool_name> output saved to <虚拟文件路径> (<字符数> chars, ~<估算数> tokens).]
[Preview kind: <类型>. This is a structured synopsis, not a raw head/tail truncation.]
```

后接结构摘要、示例和读取建议；完整内容需按引用路径读取。写入类历史结果还可能在后续模型请求中被省略，不能把“当前模型上下文里看不到”当成“工具没有执行”。

无法持久化全文时，回退截断标记通常为：

```text
[... <N> chars omitted from <tool_name> output. Persistent storage unavailable. Consider narrowing the query or using more specific parameters.]
```

预算过小时可能只保留截短文本而放不下这条标记；Bash 退出码会尽可能保留。

相关实现：[异常处理](../deerflow/agents/middlewares/tool_error_handling_middleware.py)、[读后写保护](../deerflow/agents/middlewares/read_before_write_middleware.py)、[输出预算](../deerflow/agents/middlewares/tool_output_budget_middleware.py)、[摘要格式](../deerflow/agents/middlewares/tool_output_synopsis.py)。

本文按源码覆盖正常／异常返回分支，未在本次重跑真实模型、外部 API 或所有沙箱 provider；外部异常详情及依赖库校验文案以运行环境为准。
