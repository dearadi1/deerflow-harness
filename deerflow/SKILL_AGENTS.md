# DeerFlowSkillClient 接口说明

本文对应 [skill_client.py](skill_client.py) 中的 `DeerFlowSkillClient`。原有接口管理指定用户的 `custom` 技能；新增 `client.public` 入口管理不绑定用户的全局 `public` 技能。两类操作共用技能包校验和文件操作规则。

## 初始化与通用输入

先设置 `DEERFLOW_CONFIG_PATH`，例如 `export DEERFLOW_CONFIG_PATH=/absolute/path/config.yaml`：

```python
from deerflow import DeerFlowSkillClient

client = DeerFlowSkillClient()
```

也可以显式指定配置文件：

```python
client = DeerFlowSkillClient(config_path="/absolute/path/config.yaml")
```

| 输入 | 类型 | 说明 |
| --- | --- | --- |
| `config_path` | `str \| None` | 显式路径优先；省略或为 `None` 时读取 `DEERFLOW_CONFIG_PATH`，两者均未提供或环境变量为空时抛出 `ValueError`。相对路径按调用方工作目录解析，不再自动查找根目录配置。 |
| `user_id` | `str` | 必填，非空用户标识 |
| `name` / `skill_name` | `str` | 技能名称，移除首尾空白后为 1～64 个小写字母、数字或连字符 `-`；不能以连字符开头或结尾，不能包含连续连字符 |
| `path` / `new_path` | `str` | 相对于该技能目录的路径，例如 `scripts/main.py`；使用 `/` 分隔，不能是空路径、技能根目录、绝对路径，不能包含 `..`、反斜杠或 NUL 字符 |

初始化输出为 `DeerFlowSkillClient` 实例。配置不存在可能抛出 `FileNotFoundError`；配置或环境变量无效可能抛出 `ValueError`、配置校验异常；权限问题可能抛出 `PermissionError`。

通用调用约定：

- 创建文件、创建文件夹、移动资源时，父目录必须已经存在。
- 根目录的 `SKILL.md` 由 `create_skill` 创建，允许下载和通过 `upload_file` 更新；不能通过普通文件接口单独创建、删除或移动，也不能将其他资源移到该位置。保护包含根目录下的大小写变体和指向入口文件的硬链接别名。
- 删除整个技能使用 `delete_skill`。
- 资源读写不允许通过符号链接访问。`get_resource_tree` 可以展示链接节点，但不递归展开目标目录。
- 以下接口均为同步方法；失败时抛出异常，不返回错误字典。

## public 技能接口

`client.public` 与当前客户端共享配置，无需额外初始化或传入 `user_id`。public 原始目录为配置解析得到的 `<skills 根目录>/public/`；该根目录沿用 `skills.path`、`DEER_FLOW_SKILLS_PATH` 等现有解析规则。custom 原始目录仍为 `<数据目录>/users/<user_id>/skills/custom/`。

下表列出 public 的全部 16 个同步接口。除复制方向和删除状态外，参数校验、返回结构和异常规则与下文对应的 custom 接口相同。

| public 方法签名 | 输出与行为 |
| --- | --- |
| `create_skill(name: str, description: str, **extra_fields: str) -> str` | 创建 public 技能及 `SKILL.md`，返回实际技能目录绝对路径 |
| `copy_skill(skill_name: str, target_user_id: str) -> str` | 将 public 技能复制到目标用户的 custom，返回目标技能目录绝对路径 |
| `delete_skill(skill_name: str) -> None` | 删除 public 技能目录；保留全局按名称共享的启用配置和各用户的状态、历史及 custom 技能 |
| `get_skill_info(skill_name: str) -> dict[str, Any]` | 返回完整 YAML 头，保留原始字段类型 |
| `list_skills() -> list[dict[str, Any]]` | 仅列出 public，包含禁用项，返回最终启用状态 `enabled`，跳过无效文档，按名称排序 |
| `list_skill_states() -> list[dict[str, Any]]` | 返回 public 的名称、全局开关及最终启用状态 |
| `set_skill_enabled(skill_name: str, enabled: bool) -> dict[str, Any]` | 修改全局 public 开关，返回修改后的状态 |
| `get_resource_tree(skill_name: str) -> list[dict[str, Any]]` | 返回该 public 技能的资源树 |
| `create_file(skill_name: str, path: str) -> None` | 创建空文件，父目录必须存在 |
| `create_folder(skill_name: str, path: str) -> None` | 创建空目录，父目录必须存在 |
| `delete_file(skill_name: str, path: str) -> None` | 删除普通文件 |
| `delete_folder(skill_name: str, path: str) -> None` | 递归删除目录 |
| `move_file(skill_name: str, path: str, new_path: str) -> None` | 移动或重命名文件，不覆盖已有目标 |
| `move_folder(skill_name: str, path: str, new_path: str) -> None` | 移动或重命名目录，不覆盖已有目标 |
| `upload_file(skill_name: str, path: str, file_bytes: bytes) -> None` | 用无 NUL 的 UTF-8 文本覆盖已有文件；也接受 `bytearray` |
| `download_file(skill_name: str, path: str) -> bytes` | 返回文件原始字节 |

```python
public_path = client.public.create_skill("shared-helper", "公共助手")
client.public.create_folder("shared-helper", "scripts")
client.public.create_file("shared-helper", "scripts/main.py")
client.public.upload_file("shared-helper", "scripts/main.py", b"print('hello')\n")
info = client.public.get_skill_info("shared-helper")
skills = client.public.list_skills()
tree = client.public.get_resource_tree("shared-helper")
content = client.public.download_file("shared-helper", "scripts/main.py")

# 用户得到独立副本，随后可以通过原有 custom 接口自行修改。
custom_path = client.public.copy_skill(skill_name="shared-helper", target_user_id="lyj")
client.upload_file("lyj", "shared-helper", "scripts/main.py", b"print('custom')\n")
client.public.delete_skill("shared-helper")
```

`copy_skill(skill_name, target_user_id)` 固定从 public 复制到指定用户的 custom。保留技能名称和包内所有普通资源，包括隐藏文件、空目录和二进制文件；自动创建目标用户缺失的 custom 根目录。目标同名路径存在时抛出 `FileExistsError`，不覆盖、不合并；源文档无效、包内包含符号链接或特殊文件时拒绝复制。复制后的文件独立，用户修改或 public 后续删除均不会改变另一份资源。此接口不复制启用状态和历史记录；目标仍受现有全局按名称配置及该用户自身状态约束，不保证复制后启用。

public 列表项与 custom 使用相同字段，其中 `skill_path` 为 `<容器技能根目录>/public/<技能相对目录>`，`host_path` 为实际目录绝对路径，`enabled` 为布尔值，表示该技能当前的全局启用状态。public 和 custom 同名时，各管理入口只操作自身目录；运行时继续遵循现有的用户 custom 优先规则。

public 写入会在全局 public 投影锁内修改并重建沙箱视图，成功后失效本进程已加载的技能提示词缓存，并发布全局共享缓存失效标记；public 复制到 custom 后仅通知目标用户的缓存。共享标记只有在能解析到 extensions 配置路径时才会发布，其他进程在后续缓存检查时识别，通常有至多约一秒的轮询延迟；不共享配置目录的进程不会收到通知。已开始运行的快照遵循原有运行时行为。

如果文件已修改成功、共享缓存失效通知随后发布失败，接口会抛出 `RuntimeError("技能已修改，但共享技能缓存失效通知发布失败")`；此时先查询实际内容，不要将异常当成修改未发生。public 管理要求全局本地文件存储实现，其他存储实现会抛出 `TypeError`。public 写入面向全局管理调用方，应用层自行控制调用权限。

## custom 技能管理与查询

### create_skill

```text
create_skill(user_id: str, name: str, description: str, **extra_fields: str) -> str
```

**输入：** `user_id`、`name` 遵循通用约定；`description` 为非空字符串；`extra_fields` 为写入 YAML 头的其他字段，各字段值必须是字符串。

**输出：** 新技能实际存储目录的绝对路径，不包含 `SKILL.md`。自动生成包含 YAML 头和初始正文的 `SKILL.md`。

```python
skill_path = client.create_skill(
    user_id="lyj",
    name="test-skill",
    description="测试技能",
    tags="test, skill",
    license="MIT",
)
```

返回路径形式为 `<数据目录绝对路径>/users/lyj/skills/custom/test-skill`，具体位置取决于当前配置。`tags="test, skill"` 保存为字符串，不会自动转换成列表。带连字符的 YAML 字段可以通过 `**{"allowed-tools": "Read Bash"}` 传入。

**可能异常：** 同名路径已存在时抛出 `FileExistsError`；名称、描述或已知 YAML 字段格式无效时抛出 `ValueError`；名称或额外字段类型错误时抛出 `TypeError`；写入失败可能抛出 `PermissionError`、`OSError`。

### copy_skill

```text
copy_skill(source_user_id: str, target_user_id: str, skill_name: str) -> str
```

**输入：** `source_user_id` 为来源用户，`target_user_id` 为目标用户，均为非空字符串；`skill_name` 为来源用户已有的 custom 技能名称。

**输出：** 目标用户的新技能目录绝对路径，不包含 `SKILL.md`。保持技能名称，复制包内全部资源，包括隐藏文件、空目录和二进制文件。目标用户的 custom 目录不存在时自动创建。来源用户的技能历史和启用状态不复制。

```python
target_path = client.copy_skill(
    source_user_id="lyj",
    target_user_id="lyj2",
    skill_name="test-skill",
)
```

**可能异常：** 来源技能或 `SKILL.md` 不存在时抛出 `FileNotFoundError`；目标同名路径已存在时抛出 `FileExistsError`，不会覆盖或合并；源 YAML 无效、名称不一致，或包内包含符号链接、特殊文件时抛出 `ValueError`；复制失败还可能抛出 `UnicodeDecodeError`、`shutil.Error`、`OSError`。复制给同一用户也会因同名路径已存在而失败。

### delete_skill

```text
delete_skill(user_id: str, skill_name: str) -> None
```

**输入：** 用户标识和技能名称。

**输出：** `None`。删除整个技能目录、该技能历史记录和该用户对应的启用状态。

```python
client.delete_skill(user_id="lyj2", skill_name="test-skill")
```

**可能异常：** 技能目录不存在时抛出 `FileNotFoundError`；路径类型错误时抛出 `NotADirectoryError`；名称或路径校验失败时抛出 `ValueError`；删除失败可能抛出 `PermissionError`、`OSError`。

### get_skill_info

```text
get_skill_info(user_id: str, skill_name: str) -> dict[str, Any]
```

**输入：** 用户标识和技能名称。

**输出：** 完整 YAML 头，包括 `name`、`description` 和其他已有字段。保留列表、字典、布尔值等原始类型；不自动附加正文、路径或运行时启用状态。

```python
info = client.get_skill_info(user_id="lyj", skill_name="test-skill")
```

上述创建示例对应的返回值：

```python
{
    "name": "test-skill",
    "description": "测试技能",
    "tags": "test, skill",
    "license": "MIT",
}
```

**可能异常：** 技能目录或 `SKILL.md` 不存在时抛出 `FileNotFoundError`；YAML、UTF-8 编码或名称一致性校验失败时抛出 `ValueError`；`SKILL.md` 实际为目录时抛出 `IsADirectoryError`；文件持续被修改、无法取得稳定内容时抛出 `RuntimeError`。

### list_skills

```text
list_skills(user_id: str) -> list[dict[str, Any]]
```

**输入：** 用户标识。

**输出：** 该用户可解析的 custom 技能列表，包含禁用项，按名称排序。用户尚无技能目录或没有有效技能时返回 `[]`。无效 YAML、符号链接技能目录等不会出现在结果中。

```python
skills = client.list_skills(user_id="lyj")
```

单个列表项格式：

```python
{
    "name": "test-skill",
    "description": "测试技能",
    "skill_path": "/mnt/skills/custom/test-skill",
    "host_path": "/absolute/data/users/lyj/skills/custom/test-skill",
    "enabled": True,
}
```

`skill_path` 是配置的容器路径，`host_path` 是实际存储目录的绝对路径；示例中的两个根路径都由配置决定。`enabled` 是布尔值，表示全局开关与该用户开关共同决定的最终启用状态；仅标注启用情况，不过滤禁用项，也不包含 Agent 白名单或授权策略的判断。启用状态每次查询重新读取，不随技能元数据缓存。列表不包含完整 YAML 头，获取其他字段使用 `get_skill_info`。

**可能异常：** 用户标识或存储根路径无效时抛出 `ValueError`；目录类型错误时抛出 `NotADirectoryError`；无权访问目录或文件时抛出 `PermissionError`；技能文件持续变化时可能抛出 `RuntimeError`。无效 YAML 会跳过，不会因此中断整个列表查询。

### list_skill_states 与 set_skill_enabled

```python
# custom 必须指定用户，只包含用户自有技能，不包含 legacy 或 integrations。
states = client.list_skill_states(user_id="lyj")
state = client.set_skill_enabled(user_id="lyj", skill_name="test-skill", enabled=False)

# public 无需用户参数。
public_states = client.public.list_skill_states()
public_state = client.public.set_skill_enabled(skill_name="shared-helper", enabled=True)
```

`list_skill_states` 按名称排序，返回全部技能的状态；`set_skill_enabled` 返回对应技能修改后的状态：

```python
# custom
{"name": "test-skill", "global_enabled": True, "user_enabled": False, "enabled": False}

# public
{"name": "shared-helper", "global_enabled": True, "enabled": True}
```

全局状态来自 `extensions_config.json`，按技能名称共享；用户状态来自 `<数据目录>/users/<user_id>/skills/_skill_states.json`。未配置时默认启用。public 的 `enabled` 等于 `global_enabled`；custom 的 `enabled` 等于 `global_enabled and user_enabled`，用户启用不能覆盖全局禁用，同名 custom 也受该全局开关约束。

修改 custom 仅写入指定用户的状态；修改 public 写入全局扩展配置，保留其他配置及环境变量引用。修改会更新沙箱技能视图，失效本进程技能缓存，并通过共享重置标记通知其他进程；已构建的会话 Agent 快照仍需按原有生命周期重建。

`enabled` 必须为布尔值，否则抛出 `TypeError`；指定作用域中不存在技能时抛出 `FileNotFoundError`。public 修改要求能定位到已有扩展配置，否则抛出 `FileNotFoundError`。配置格式无效时查询和修改均可能抛出配置校验异常。共享通知失败时会抛出上文所述的 `RuntimeError`，但状态可能已写入，应重新查询确认。

### get_resource_tree

```text
get_resource_tree(user_id: str, skill_name: str) -> list[dict[str, Any]]
```

**输入：** 用户标识和技能名称。

**输出：** 技能根目录下的资源节点列表，递归包含子目录资源。每层按名称忽略大小写排序，排除点号开头的目录及其子树，保留隐藏文件。

```python
tree = client.get_resource_tree(user_id="lyj", skill_name="test-skill")
```

| 节点字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `str` | 文件或目录名称 |
| `path` | `str` | 相对于技能根目录的路径，使用 `/` 分隔 |
| `type` | `str` | `folder` 或 `file` |
| `content_type` | `str \| None` | 目录为 `None`；文件按名称推断 MIME 类型，无法识别时为 `application/octet-stream` |
| `children` | `list[dict[str, Any]] \| None` | 子节点；文件和空目录为 `None` |

单个目录节点示例：

```python
{
    "name": "references",
    "path": "references",
    "type": "folder",
    "content_type": None,
    "children": [
        {
            "name": "readme.txt",
            "path": "references/readme.txt",
            "type": "file",
            "content_type": "text/plain",
            "children": None,
        }
    ],
}
```

**可能异常：** 技能目录不存在时抛出 `FileNotFoundError`；根路径不是目录时抛出 `NotADirectoryError`；根路径包含符号链接或名称非法时抛出 `ValueError`；遍历失败可能抛出 `PermissionError`、`OSError`。包内符号链接仅展示为文件节点，不遍历目标。

## 文件与文件夹接口

以下接口的 `user_id`、`skill_name`、`path`、`new_path` 均遵循通用输入约定。表中列出特定操作的异常；通用参数校验和文件系统异常见文末。

| 方法签名 | 输入补充 | 输出 | 主要异常 |
| --- | --- | --- | --- |
| `create_file(user_id: str, skill_name: str, path: str) -> None` | 父目录必须存在，目标路径必须不存在 | `None`，创建空文件 | 目标存在：`FileExistsError`；父目录缺失：`FileNotFoundError` |
| `create_folder(user_id: str, skill_name: str, path: str) -> None` | 父目录必须存在，不会递归补齐父目录 | `None`，创建空文件夹 | 目标存在：`FileExistsError`；父目录缺失：`FileNotFoundError` |
| `delete_file(user_id: str, skill_name: str, path: str) -> None` | 目标必须是普通文件 | `None`，删除文件 | 文件不存在：`FileNotFoundError`；目标是目录：`IsADirectoryError` |
| `delete_folder(user_id: str, skill_name: str, path: str) -> None` | 目标必须是目录 | `None`，递归删除目录和全部内容 | 目录不存在：`FileNotFoundError`；目标不是目录：`NotADirectoryError` |
| `move_file(user_id: str, skill_name: str, path: str, new_path: str) -> None` | 源文件存在、目标不存在、目标父目录存在 | `None`，移动文件 | 源或父目录不存在：`FileNotFoundError`；目标存在：`FileExistsError`；源是目录：`IsADirectoryError` |
| `move_folder(user_id: str, skill_name: str, path: str, new_path: str) -> None` | 源目录存在、目标不存在、目标父目录存在 | `None`，移动目录及其内容 | 源或父目录不存在：`FileNotFoundError`；目标存在：`FileExistsError`；源不是目录：`NotADirectoryError`；移入自身子目录：`ValueError` |
| `upload_file(user_id: str, skill_name: str, path: str, file_bytes: bytes) -> None` | 文件必须已存在；接受 `bytes` 或 `bytearray`，内容必须为 UTF-8 文本，不能含 NUL 字符 | `None`，覆盖已有文件内容 | 文件不存在：`FileNotFoundError`；目标是目录：`IsADirectoryError`；输入类型错误：`TypeError`；编码无效：`UnicodeDecodeError`；内容校验失败：`ValueError` |
| `download_file(user_id: str, skill_name: str, path: str) -> bytes` | 目标必须是普通文件 | 原始文件字节，可包含二进制内容 | 文件不存在：`FileNotFoundError`；目标是目录：`IsADirectoryError` |

以下示例可以在已存在的 `test-skill` 中顺序执行，要求 `scripts` 和 `scripts-copy` 尚不存在：

```python
client.create_folder(user_id="lyj", skill_name="test-skill", path="scripts")
client.create_file(user_id="lyj", skill_name="test-skill", path="scripts/main.py")

client.upload_file(
    user_id="lyj",
    skill_name="test-skill",
    path="scripts/main.py",
    file_bytes="print('你好')\n".encode("utf-8"),
)
file_bytes = client.download_file(
    user_id="lyj", skill_name="test-skill", path="scripts/main.py"
)

client.move_file(
    user_id="lyj", skill_name="test-skill",
    path="scripts/main.py", new_path="scripts/run.py",
)
client.move_folder(
    user_id="lyj", skill_name="test-skill",
    path="scripts", new_path="scripts-copy",
)
client.delete_file(user_id="lyj", skill_name="test-skill", path="scripts-copy/run.py")
client.delete_folder(user_id="lyj", skill_name="test-skill", path="scripts-copy")
```

更新 `SKILL.md` 时必须上传完整文档，YAML 的 `name` 必须与技能目录名一致，`description` 必须是非空字符串，已知字段需要符合各自格式：

```python
content = """---
name: test-skill
description: 更新后的测试技能
tags:
  - test
  - skill
---

# 使用说明

这里填写技能正文。
"""
client.upload_file(
    user_id="lyj",
    skill_name="test-skill",
    path="SKILL.md",
    file_bytes=content.encode("utf-8"),
)
```

## 通用异常与报错示例

异常类型保留为 Python 标准类型或底层库类型。常见文件系统错误保留错误码与路径，错误原因转换为中文；配置加载等底层异常可能保留原始提示。

| 异常类型 | 常见触发条件 | 报错示例或提示片段 |
| --- | --- | --- |
| `TypeError` | 技能名称不是字符串、额外 YAML 字段不是字符串、上传参数不是字节数据 | `技能名称必须是字符串`；`extra_fields 的值必须是字符串`；`上传内容必须是字节数据（bytes 或 bytearray）` |
| `ValueError` | 用户标识为空、名称不合法、路径越界或包含符号链接 | `user_id 必须是非空字符串`；`path 不得指向技能根目录、绝对路径或包含 ..`；`不允许通过符号链接操作技能` |
| `ValueError` | 对根目录 `SKILL.md` 执行受保护的创建、删除或移动操作 | `SKILL.md 是技能入口文件，不能单独创建、删除或移动；请使用 upload_file 更新内容，或使用 delete_skill 删除整个技能` |
| `ValueError` | YAML 头缺失、语法错误、名称不一致或字段格式错误 | `SKILL.md 缺少有效的 YAML 头`；`SKILL.md 的 name 必须与目录名 ... 一致`；`SKILL.md 的 description 必须是非空字符串` |
| `FileNotFoundError` | 指定技能、源文件或父目录不存在 | `文件或目录不存在` |
| `FileExistsError` | 创建或移动的目标已经存在 | `目标文件或目录已存在` |
| `NotADirectoryError` | 应为目录的路径实际是文件 | `指定路径不是文件夹` |
| `IsADirectoryError` | 将目录作为文件上传、下载或删除 | `指定路径是文件夹，不能作为文件操作` |
| `UnicodeDecodeError` | 上传或复制时读取到非 UTF-8 的技能文本 | `内容不是有效的 UTF-8 文本，请检查文件编码` |
| `UnicodeEncodeError` | 写入文本含无法用 UTF-8 编码的字符 | `文本包含无法使用 UTF-8 编码的字符` |
| `PermissionError` | 文件或目录权限不足 | `没有访问该文件或目录的权限`；`系统不允许执行此操作` |
| `OSError` | 磁盘满、只读文件系统、跨文件系统移动等 | `磁盘空间不足`；`文件系统为只读，无法修改`；`无法跨文件系统移动文件或文件夹` |
| `shutil.Error` | 复制目录资源失败 | `复制技能资源失败，请检查源文件权限和目标磁盘空间` |
| `RuntimeError` | 查询期间技能文件持续变化，或底层技能视图更新失败 | `技能文件持续发生变化，暂时无法读取技能信息，请稍后重试`；底层更新错误可能保留原始提示 |

查询详情时，非 UTF-8 的 `SKILL.md` 会转换为 `ValueError("SKILL.md 必须使用有效的 UTF-8 编码")`；列表查询会跳过该技能。`UnicodeDecodeError`、`UnicodeEncodeError` 均属于 `ValueError` 的子类；`FileNotFoundError`、`FileExistsError`、`PermissionError` 等属于 `OSError` 的子类。
