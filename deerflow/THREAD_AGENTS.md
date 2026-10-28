# DeerFlowThreadClient 接口说明

模块：`deerflow.thread_client`。所有接口均为同步调用，存储路径沿用 DeerFlow 当前配置。

## 初始化

先设置 `DEERFLOW_CONFIG_PATH`，例如 `export DEERFLOW_CONFIG_PATH=/absolute/path/config.yaml`：

```python
from deerflow.thread_client import DeerFlowThreadClient

manager = DeerFlowThreadClient(
    config_path=None,
    checkpointer=None,
)
```

| 输入 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `config_path` | `str \| None` | `None` | 显式路径优先；省略或为 `None` 时读取 `DEERFLOW_CONFIG_PATH`，两者均未提供或环境变量为空时抛出 `ValueError`。相对路径按调用方工作目录解析，不再自动查找根目录配置。同一进程的管理接口应使用同一个配置文件。 |
| `checkpointer` | `BaseCheckpointSaver \| None` | `None` | 同步 checkpoint 存储实例；省略时按现有配置获取默认实例。若 `DeerFlowClient` 使用自定义实例，此处必须传入同一个实例。 |

输出：`DeerFlowThreadClient` 实例。

## 通用输入

| 参数 | 类型 | 要求 |
| --- | --- | --- |
| `user_id` | `str` | DeerFlow 的存储用户 ID，非空，仅允许 ASCII 字母、数字、下划线和连字符。每次调用显式传入，不读取当前用户上下文。 |
| `thread_id` | `str` | 通常使用 `create_thread()` 的返回值；允许 1～64 个 ASCII 字母、数字、下划线或连字符。 |
| `file_name` | `str` | 单个文件名，例如 `report.pdf`；不能传入完整路径、目录分隔符或 `.`、`..`。需符合现有上传文件名校验，UTF-8 编码不超过 255 字节，不能使用上传暂存保留名称。 |
| `file_bytes` | `bytes` | 文件的二进制内容；不能传入字符串、文件对象或 `bytearray`。允许空字节串。 |

## 方法输入与输出

### 创建线程

```python
def create_thread(self, user_id: str) -> str
```

- 输入：`user_id`。
- 输出：UUID 格式的线程 ID 字符串。
- 创建线程目录及初始空 checkpoint。

### 删除线程

```python
def delete_thread(self, user_id: str, thread_id: str) -> None
```

- 输入：`user_id`、`thread_id`。
- 输出：`None`。
- 删除该线程所有 checkpoint 命名空间和整个线程目录，包括上传文件、转码文件、工作文件及输出文件。
- 指定用户的线程目录不存在时直接返回，不执行 checkpoint 删除。重复删除已清理的线程不会报错。
- 应在会话停止运行后调用；删除范围不包含 Gateway 运行记录或远程沙箱。

### 上传文件

```python
def upload_file(self, user_id: str, thread_id: str, file_name: str, file_bytes: bytes) -> None
```

- 输入：`user_id`、`thread_id`、`file_name`、`file_bytes`；线程目录必须已存在。
- 输出：`None`，不返回文件信息或转码状态。
- 同名文件会被覆盖，旧的、归属验证通过的转码 Markdown 会被清理。
- `.pdf`、`.ppt`、`.pptx`、`.xls`、`.xlsx`、`.doc`、`.docx` 按现有配置转换为 Markdown；转码文件重名时自动使用带编号的名称。
- 转换器返回失败时保留原文件，记录 `文件已上传，但 Markdown 转换失败: <文件名>` 日志，方法仍返回 `None`。

### 下载文件

```python
def download_file(self, user_id: str, thread_id: str, file_name: str) -> bytes
```

- 输入：`user_id`、`thread_id`、`file_name`；仅接受单个文件名，例如 `report.pdf`，不接受任何路径。
- 输出：原始文件的二进制内容 `bytes`，空文件返回 `b""`。
- 只读取当前用户、当前线程 `uploads` 目录的直属普通文件，不搜索子目录或其他线程。
- 文件或线程不存在时抛出 `FileNotFoundError`；目录、符号链接等非普通文件抛出 `UnsafeUploadPathError`。

### 删除文件

```python
def delete_file(self, user_id: str, thread_id: str, file_name: str) -> None
```

- 输入：`user_id`、`thread_id`、`file_name`。
- 输出：`None`。
- 删除指定文件，并清理归属记录验证通过的转码 Markdown，包括带编号的转码文件。
- 没有有效归属记录的 `.md` 不会因同名被连带删除。文件不存在时抛出 `FileNotFoundError`。

### 列出文件

```python
def thread_files(self, user_id: str, thread_id: str) -> list[str]
```

- 输入：`user_id`、`thread_id`。
- 输出：按文件名排序的列表，例如 `["report.pdf", "test.txt"]`，不包含任何目录前缀。
- 只列出 `uploads` 直属文件，不递归列出子目录。例如 `uploads/report.pdf` 返回 `"report.pdf"`，`uploads/data/result.csv` 不列入结果。
- 排除目录、符号链接、上传暂存文件及归属验证通过的转码 Markdown。用户独立上传或无法确认归属的 `.md` 仍会返回。
- 没有文件或指定用户的线程目录不存在时返回 `[]`。

## 调用示例

```python
from deerflow.thread_client import DeerFlowThreadClient

manager = DeerFlowThreadClient()
user_id = "user_001"
thread_id = manager.create_thread(user_id)

manager.upload_file(user_id, thread_id, "hello.txt", b"hello")
file_names = manager.thread_files(user_id, thread_id)

for file_name in file_names:
    file_bytes = manager.download_file(user_id, thread_id, file_name)
    manager.delete_file(user_id, thread_id, file_name)

manager.delete_thread(user_id, thread_id)
```

与 `DeerFlowClient` 共用自定义 checkpointer 时，初始化方式如下；会话调用也需使用相同的 `user_id` 和 `thread_id`。

```python
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.client import DeerFlowClient
from deerflow.thread_client import DeerFlowThreadClient

checkpointer = InMemorySaver()
manager = DeerFlowThreadClient(checkpointer=checkpointer)
client = DeerFlowClient(checkpointer=checkpointer)

thread_id = manager.create_thread("user_001")
answer = client.chat("你好", user_id="user_001", thread_id=thread_id)
```

## 可能出现的异常

| 异常 | 触发场景或报错示例 |
| --- | --- |
| `ValueError` | 用户 ID、线程 ID 或文件名不符合校验要求。例如 `user_id 必须是非空字符串`、`file_name 必须是单个文件名，不能包含目录`。底层校验也可能返回英文报错。 |
| `TypeError` | `file_bytes` 不是 `bytes`：`file_bytes 必须是 bytes`。 |
| `FileNotFoundError` | 上传、下载或删除文件时线程目录不存在：`用户线程不存在: <user_id>/<thread_id>`；下载文件不存在；删除的文件不存在或不是普通文件：`File not found: <file_name>`；配置文件缺失也可能触发。 |
| `UnsafeUploadPathError` | 线程路径中的目录是符号链接，上传或下载目标是符号链接、目录等不安全目标，或下载目标在打开时被替换。此异常继承自 `ValueError`，从 `deerflow.uploads.manager` 导入。 |
| `PathTraversalError` | 底层文件路径校验发现越界，例如符号链接指向上传目录外。此异常继承自 `ValueError`，从 `deerflow.uploads.manager` 导入。 |
| `NotADirectoryError` | 应为线程目录或上传目录的路径实际是普通文件。 |
| `PermissionError` | 删除线程时 checkpoint 的 `user_id` 明确属于其他用户：`checkpoint 属于其他用户，不能删除`；文件系统权限不足也可能触发。 |
| `FileExistsError` | 创建线程的目标目录已存在，或底层文件发布遇到无法处理的名称冲突。 |
| `ImportError` | 配置所需的 checkpoint 后端依赖未安装。 |
| `NotImplementedError` | 传入的 checkpointer 未实现所需同步方法，例如仅提供异步接口。 |
| 其他配置、存储或 `OSError` 异常 | 配置解析失败、数据库不可用、磁盘空间不足、文件读写失败等底层异常会继续向调用方抛出。 |

文件系统与 checkpoint 删除不是同一个事务。异常发生时可能已完成部分操作：例如上传原文件成功后转码文件发布失败，或 checkpoint 删除成功后线程目录清理失败；抛出异常不代表所有操作均已回滚。
