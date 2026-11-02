# 日志归档与查询

服务使用现有 Python `logging`，初始化客户端后停止实时向终端输出日志，统一保存为 UTF-8 JSON Lines。聊天返回值、流事件和查询命令的输出仍按原接口交付。

## 环境配置

`.env.example` 显式提供以下变量，部署时可在 `.env` 中填写：

```dotenv
# 留空时使用 DEER_FLOW_HOME/logs
DEER_FLOW_LOG_DIR=
DEER_FLOW_LOG_LEVEL=INFO
```

- **目录**：显式 `DEER_FLOW_LOG_DIR` > `${DEER_FLOW_HOME}/logs`。未设置 `DEER_FLOW_HOME` 时，使用 `${DEER_FLOW_PROJECT_ROOT}/.deer-flow/logs`；项目根目录缺省为启动目录。相对日志目录基于项目根目录解析。
- **等级**：非空 `DEER_FLOW_LOG_LEVEL` > `config.yaml` 中的 `log_level` > `INFO`。支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`，不区分大小写；无效值在初始化时报错。
- **生效时机**：修改环境变量后重启服务。环境文件沿用框架的 `.env` 加载方式，也支持 `DEER_FLOW_ENV_FILE=/absolute/path/service.env`。进程环境变量优先于环境文件，包括显式空值。

例如 `DEER_FLOW_HOME=/var/lib/deerflow` 时，默认文件为：

```text
/var/lib/deerflow/logs/deerflow-2026-11-02.jsonl
/var/lib/deerflow/logs/deerflow-2026-11-03.jsonl
```

文件按**服务进程所在时区**及每条记录的时间划分日期，时间戳包含时区偏移。需要中国日期时，在部署环境设置 `TZ=Asia/Shanghai`。多个本地进程直接追加当天文件，不通过重命名轮转，历史文件持续保留，不自动删除。应为日志目录挂载持久化存储，并按部署需要清理历史文件。跨主机网络文件系统的追加一致性取决于文件系统。

文件固定为 JSONL，方便逐行筛选。原有 URL 脱敏继续应用，并覆盖异常文本。`logging.enhance.enabled` 继续控制可选的 `trace_id`；旧 `logging.enhance.format` 字段保留兼容，文件格式统一为 JSONL。

## 记录范围

| 模块 / component | 操作记录 | 定位字段 |
| --- | --- | --- |
| `agent_client`、同步客户端和托管流 / `agent` | session 调用、启动等待、流执行、取消、关闭超时、异常、清理 | `user_id`、`thread_id`、`operation`、`replay_last_turn`、`event_count` |
| `thread_client` / `thread` | 创建和删除线程，上传、下载、删除、列出文件；转换等下游日志 | `user_id`、`thread_id`、`file_path`、`area`、`byte_count`、`count` |
| `skill_client` / `skill` | public/custom 技能创建、复制、删除、查询、启用状态及资源文件操作 | `user_id`、`category`、`skill_name`、`source_user_id`、`target_user_id`、文件路径及大小 |

操作记录包含 `started`、`completed`、`failed` 或 `cancelled` 状态，结束记录包含 `duration_ms`。异常保留堆栈和 `error_type`。`start_stream` 完成表示启动成功，整轮结果以 `managed_stream` 的结束记录为准；`session.open_stream` 表示创建迭代器，后续执行以 `session.stream` / `stream` 记录为准。

下游模型、工具和存储的标准日志在执行期间继承用户、线程及操作信息；同步流每次推进和关闭时绑定，挂起期间恢复调用方上下文，托管流在后台执行和清理任务中保持绑定。技能管理没有会话线程，`thread_id` 为 `null`；全局 public 操作的 `user_id` 为 `null`，复制操作额外记录目标用户。初始化等没有用户或线程的日志也使用 `null`。

新增操作日志只记录定位信息和结果，不主动写入聊天正文、技能描述、文件内容、返回内容或任意配置参数。已有底层日志及异常仍按其原有内容记录。

## 查看线程和日志

从服务的项目目录运行，或传入与服务相同的环境变量；查询不需要模型凭据、配置文件、数据库或正在运行的服务。

```bash
# 简写：日期 + 用户，列出当天在日志中出现过的线程
uv run deerflow logs 2026-11-02 user_001

# 简写：日期 + 用户 + 线程，直接打印相关日志
uv run deerflow logs 2026-11-02 user_001 thread_001

# 对应的完整命令
uv run deerflow logs threads --date 2026-11-02 --user-id user_001
uv run deerflow logs show --date 2026-11-02 --user-id user_001 --thread-id thread_001

# 仅查看线程文件操作
uv run deerflow logs show --date 2026-11-02 --user-id user_001 --component thread

# 查看指定线程的 ERROR 日志（等级为精确匹配）
uv run deerflow logs show --date 2026-11-02 --user-id user_001 --thread-id thread_001 --level ERROR

# 查询技能列表及具体技能的失败操作
uv run deerflow logs skills --date 2026-11-02 --user-id user_001
uv run deerflow logs show --date 2026-11-02 --user-id user_001 --component skill --skill-name example --status failed

# 查看不绑定用户的全局 public 技能操作
uv run deerflow logs show --date 2026-11-02 --component skill --category public
```

用户、线程、操作名称、技能及等级都精确匹配。技能复制按来源用户或目标用户均可查到。`threads` / `skills` 列表统计日志条数、错误条数及首次/最后时间，只表示当天有日志的对象，不是完整的数据库对象列表。

## 导出与更多过滤

```bash
# 默认直接打印；--output 保存到文件，正文和堆栈保留
uv run deerflow logs 2026-11-02 user_001 thread_001 --output ./exports/thread_001.log

# JSONL 导出，适合进一步自动处理
uv run deerflow logs show --date 2026-11-02 --user-id user_001 --thread-id thread_001 --format json --output ./exports/thread_001.jsonl

# 单独指定目录，或精确查询某个操作
uv run deerflow logs show --date 2026-11-02 --log-dir /var/lib/deerflow/logs --component thread --operation upload_file

uv run deerflow logs --help
uv run deerflow logs show --help
```

所有子命令都支持 `--log-dir`、`--output`、`--format text|json` 及通用过滤参数。导出采用临时文件完成后替换目标，禁止覆盖源日志。源文件不存在返回退出码 `1`；无匹配记录时成功返回且输出为空；损坏或尚未写完的 JSON 行会被跳过并在标准错误中报告数量。
