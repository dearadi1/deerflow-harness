# Skill 管理器使用指南

`DeerFlowSkillClient` 管理公共技能包、公共预设和用户私有技能包。实现见 [skill_client.py](skill_client.py)，逐格调用示例见 [demo4.ipynb](../demo4.ipynb)。

| 入口 | 管理范围 | 启用状态 |
| --- | --- | --- |
| `client.public` | 所有用户共享的 public 技能，无需 `user_id` | 在公共预设中配置和查询 |
| `client` | 指定 `user_id` 自有的 custom 技能 | 用户开关与全局按名称配置共同决定 |

## 初始化与目录

```python
from deerflow import DeerFlowSkillClient

client = DeerFlowSkillClient(config_path="config.yaml")
public = client.public
```

`config_path` 显式传入时优先；省略或为 `None` 时读取 `DEERFLOW_CONFIG_PATH`。两者均未提供或环境变量为空时抛出 `ValueError`。相对路径按当前工作目录解析。同一进程的 Skill 管理器和 Agent 池应使用同一个配置文件。

| 内容 | 位置 |
| --- | --- |
| 公共技能原始文件 | `<skills 根目录>/public/<skill_name>/`，根目录由 `skills.path`、`DEER_FLOW_SKILLS_PATH` 等配置解析 |
| 私有技能原始文件 | `<数据目录>/users/<user_id>/skills/custom/<skill_name>/` |
| 公共预设文件 | `<数据目录>/skill_presets.json`，可通过 `DEER_FLOW_SKILL_PRESETS_PATH` 覆盖 |

数据目录由 `DEER_FLOW_HOME` 指定，未指定时为调用方项目下的 `.deer-flow`。管理器使用本地文件存储；创建和复制返回宿主机实际目录的绝对路径，不包含 `SKILL.md`。

## 公共技能

### 创建、查询、复制和删除

```python
public.create_skill(name="shared-summary", description="整理资料并生成摘要", tags="summary")
public.get_skill_info(skill_name="shared-summary")
public.list_skills()
```

| 方法 | 返回值与行为 |
| --- | --- |
| `create_skill(name, description, **extra_fields)` | 创建技能包和 `SKILL.md`，返回目录路径字符串 |
| `get_skill_info(skill_name)` | 完整 YAML 头，保留字段类型 |
| `list_skills()` | 按名称排序的公共技能列表，仅包含技能信息 |
| `copy_skill(skill_name, target_user_id)` | 复制为目标用户的私有技能，返回目标目录路径字符串 |
| `delete_skill(skill_name)` | 删除公共技能包并清除所有预设中的同名开关，返回 `None` |

公共列表项的结构如下，不接收预设参数，也不返回 `enabled` 或 `preset_id`：

```python
{
    "name": "shared-summary",
    "description": "整理资料并生成摘要",
    "skill_path": "/mnt/skills/public/shared-summary",
    "host_path": "/absolute/skills/public/shared-summary",
}
```

示例路径随配置变化。`skill_path` 是容器路径，`host_path` 是宿主机原始目录。查询某个预设的启用情况使用 `get_preset()`，查询所有预设使用 `list_presets()`。

```python
public.copy_skill(skill_name="shared-summary", target_user_id="user_001")
```

复制后的文件独立，目标同名路径存在时抛出 `FileExistsError`，不覆盖或合并。复制保留名称和资源，不复制启用状态或历史记录。删除公共包会清除所有预设中的同名开关（包括显式关闭项），不删除用户的私有副本或其状态。通过管理器新增或重新创建公共包时，将内置 `default` 中缺少的开关写为 `true`，已有显式开关保持不变；自定义预设的同名残留开关仍会清除，默认关闭。因此经过管理器删除后重建会在内置 `default` 中启用，而外部移除后重建会保留该预设仍存在的显式关闭项。

修改 `SKILL.md` 的描述、正文或其他字段，以及资源文件的增删改查，均保留已有预设选择；管理器不支持通过修改 `name` 重命名技能。重复创建、无效文档等校验失败不会改写预设。公共删除先暂存目录，预设保存失败时恢复原包；预设更新与目录增删共用事务锁，避免并发更新重新写入已删除技能的开关。

公共和私有入口只操作各自目录。对同一用户可见的同名技能，运行时优先使用该用户的私有技能。

## 公共技能预设

### 创建与使用

内置 `default` 是固定的默认预设，不能删除，可以修改名称和技能开关。首次启动或初始化恢复不合规配置时，全部有效公共技能以 `true` 写入该预设。后续新增公共技能也会默认启用并写入 JSON：通过管理器创建时立即保存；直接放入公共目录的技能，在下次初始化、预设读取或新一轮聊天解析时补齐。同步只向 `default` 添加缺少的 `true` 开关，不修改其他预设，也不覆盖已有 `false`。可通过 `public.update_preset("default", skill_states={"shared-summary": False})` 逐个关闭。

创建自定义预设时只传名称，返回含随机 `preset_id` 的详情。新预设未配置任何技能，默认全部关闭；开启技能需单独调用 `update_preset()`。聊天使用自定义预设时必须显式传入其 ID；不传或传 `None` 固定使用 `default`，没有切换默认预设的接口或配置项。

```python
preset = public.create_preset(name="资料整理")
preset_id = preset["preset_id"]

public.update_preset(
    preset_id,
    skill_states={"shared-summary": True},
)
public.get_preset(preset_id)
public.list_presets()
```

示例要求 `shared-summary` 公共包存在。预设名称可重复，ID 为随机生成的 32 位小写十六进制字符串；名称相同也不会影响内置 `default` 的身份。

| 方法 | 返回值与行为 |
| --- | --- |
| `create_preset(name)` | 空预设详情；不接收 ID、技能开关或复制来源 |
| `get_preset(preset_id=None)` | 指定预设详情；省略或传 `None` 时读取 `default` |
| `list_presets()` | `{"presets": [详情, ...]}`，列表按 ID 排序 |
| `update_preset(preset_id, *, skill_states, name=None)` | 更新后的详情；只修改传入的技能开关，未指定项保留 |
| `delete_preset(preset_id)` | 删除自定义预设，返回 `None`，不删除技能包；`default` 不可删除 |

单个详情的结构：

```python
{
    "preset_id": "3b28b1fb201c46d2894430e528a9c68d",
    "name": "资料整理",
    "is_default": False,
    "skills": {"shared-summary": True},
    "enabled_skills": ["shared-summary"],
    "missing_skills": [],
}
```

`skills` 只包含当前有效的公共技能：内置 `default` 的未配置项为 `True` 并会补写文件，其他预设的未配置项为 `False`；显式配置优先。`enabled_skills` 和运行快照也只包含实际存在且启用的公共技能。`missing_skills` 用于报告历史残留配置、外部移除或文档损坏导致的缺失项；查询不会自动清除这些配置，避免临时缺失造成选择丢失。管理器删除公共包时清除所有预设同名项；重新创建时保留内置 `default` 的显式选择，只清除自定义预设的同名残留。

`is_default` 是只读标识，仅 ID 为 `default` 时为 `True`。只修改名称时传 `skill_states={}`。开关值必须是布尔值，接口只能修改实际存在的公共技能。名称须为 1～100 个字符的非空字符串。

内置 `default` 无法删除，即使已关闭全部技能或修改名称。自定义预设可直接删除：

```python
temporary = public.create_preset(name="临时预设")
public.delete_preset(temporary["preset_id"])
```

### 文件位置与手工修改

默认文件为 `.deer-flow/skill_presets.json`；设置 `DEER_FLOW_HOME` 时，跟随该数据目录。自定义位置使用：

```dotenv
DEER_FLOW_SKILL_PRESETS_PATH=/absolute/path/skill_presets.json
```

相对路径按当前工作目录解析。预设文件必须独立于 `config.yaml`，文件名不能为 `config.yaml` 或 `config.yml`。可用下面的变量查看路径：

```python
import os
from pathlib import Path
from deerflow.config.paths import get_paths

preset_path = Path(
    os.environ.get("DEER_FLOW_SKILL_PRESETS_PATH")
    or get_paths().base_dir / "skill_presets.json"
).resolve()
```

客户端或池初始化时读取并校验文件。文件缺失时创建 ID 为 `default` 的预设，扫描当前有效公共技能并将每个技能以 `true` 显式写入 `skills`，使文件、预设查询和聊天运行状态一致，不迁移旧全局开关。仅当没有有效公共技能时，初始 `skills` 才为空。结构有效但缺少技能项时，只补齐 `default` 未记录的公共技能，已有显式选择、缺失项及自定义预设保持不变。没有待补项时不重写文件。旧全局开关仍用于私有技能，不会被修改。

文件格式示例（初始化时存在公共技能 `shared-summary`）：

```json
{
  "version": 1,
  "presets": {
    "default": {
      "name": "默认预设",
      "skills": {"shared-summary": true}
    }
  }
}
```

顶层必须且只能包含 `version` 和 `presets`，`version` 必须为整数 `1`；`presets` 必须包含 `default`，每个预设必须有 `name` 和布尔值字典 `skills`。可以手工编辑后重启服务加载；后续预设查询和新轮次也会读取文件变化。自定义 ID 可为 1～64 个小写字母、数字、下划线或连字符，首字符须为字母或数字。

客户端、池或 Skill 管理器初始化时校验文件编码、JSON 语法及预设结构。格式错误、缺少必需结构（包括内置 `default`），或仍包含旧格式的额外顶层字段时，先将原始字节备份到同目录的 `skill_presets.json.invalid-<随机值>.bak`，再恢复为仅含 `default` 的新配置，全部有效公共技能显式写为 `true`。该恢复不会保留原有关闭项或自定义预设，它们仍可从备份查看。备份文件不会覆盖已有备份；私有 Skill 状态不会被修改。使用 `DEER_FLOW_SKILL_PRESETS_PATH` 时，备份跟随指定文件的目录和文件名。备份或替换失败时抛出异常，保留原文件。

接口修改及新增公共技能的自动补齐均在文件锁内保存，写入前重读配置并重扫公共目录，避免覆盖并发关闭项或重新写回已删除的技能。保存失败时抛出异常并保留原文件；管理器创建失败会同时撤销新包，外部新增项可在后续读取时重试。运行中查询或修改遇到格式错误仍会报错，需修正文件或重新初始化后恢复；不会在业务请求中静默重置。文件缺失时创建全部启用的内置默认预设。手工编辑时应暂停其他写入者；多进程需共享同一文件及支持文件锁的文件系统。容器应挂载父目录，以支持原子替换文件。

### 对话中选择预设

```python
with pool.start_stream(
    "整理资料",
    user_id="user_001",
    skill_preset=preset_id,
) as events:
    for event in events:
        print(event.model_dump(mode="json"))
```

`pool` 的初始化及沙箱要求见 [AGENT_AGENTS.md](AGENT_AGENTS.md#每轮选择公共技能预设)。此处使用仍然存在的预设 ID。

省略 `skill_preset` 或传 `None` 时，每轮固定读取 ID 为 `default` 的预设；显式选择只影响本轮，不继承到下一轮。公共开关在一轮执行期间固定，修改影响后续轮次，已有池会自动识别。私有技能不参与预设。

## 私有技能

### 创建与查询

```python
client.create_skill(
    user_id="user_001",
    name="personal-notes",
    description="个人笔记习惯",
    tags="notes",
)
client.get_skill_info(user_id="user_001", skill_name="personal-notes")
client.list_skills(user_id="user_001")
```

| 方法 | 返回值与行为 |
| --- | --- |
| `create_skill(user_id, name, description, **extra_fields)` | 创建私有技能包，返回目录路径字符串 |
| `get_skill_info(user_id, skill_name)` | 完整 YAML 头及最终 `enabled` 状态 |
| `list_skills(user_id)` | 用户自有 custom 技能列表，包含禁用项，按名称排序 |
| `list_skill_states(user_id)` | 各技能的用户开关、全局开关及最终状态 |
| `set_skill_enabled(user_id, skill_name, enabled)` | 更新用户开关，返回该技能的状态 |
| `copy_skill(source_user_id, target_user_id, skill_name)` | 复制为目标用户的独立私有包，返回目标目录路径字符串 |
| `delete_skill(user_id, skill_name)` | 删除私有包、该用户的启用记录及历史，返回 `None` |

私有详情示例：

```python
{
    "name": "personal-notes",
    "description": "个人笔记习惯",
    "tags": "notes",
    "enabled": True,
}
```

私有列表包含 `name`、`description`、`skill_path`、`host_path`、`enabled`，不包含完整 YAML 头。两个查询的 `enabled` 都在每次调用时重新计算；即使 YAML 头声明了同名字段，返回值也以实际启用状态为准。

### 开关、复制与删除

```python
client.set_skill_enabled(user_id="user_001", skill_name="personal-notes", enabled=False)
client.list_skill_states(user_id="user_001")
```

状态结构：

```python
{
    "name": "personal-notes",
    "global_enabled": True,
    "user_enabled": False,
    "enabled": False,
}
```

最终状态为 `global_enabled and user_enabled`。全局开关来自 `extensions_config.json` 按名称配置，用户开关属于指定用户；未配置时默认开启。设置用户开关为 `True` 不会绕过全局禁用。这里的 `enabled` 不包含 Agent 白名单或授权策略的判断。

```python
client.copy_skill(
    source_user_id="user_001",
    target_user_id="user_002",
    skill_name="personal-notes",
)
client.delete_skill(user_id="user_002", skill_name="personal-notes")
```

复制不继承来源用户的开关和历史，目标同名路径存在时报错。删除一个用户的技能不影响其他用户或公共包。私有接口只管理该用户的 custom 技能，不包含 legacy 或 integrations。

## 文件与目录操作

以下方法在两个入口均可用。私有调用先传 `user_id`；公共调用省略该参数，其余参数相同。路径都相对于技能包根目录。

| 方法参数（省略私有 `user_id`） | 返回值与行为 |
| --- | --- |
| `get_resource_tree(skill_name)` | 资源节点列表 |
| `create_file(skill_name, path)` | 创建空文件；父目录须存在，目标不能已存在 |
| `create_folder(skill_name, path)` | 创建空目录；父目录须存在，目标不能已存在 |
| `upload_file(skill_name, path, file_bytes)` | 覆盖已有 UTF-8 文本文件；接收 `bytes` 或 `bytearray` |
| `download_file(skill_name, path)` | 原始 `bytes`，可读取二进制资源 |
| `move_file(skill_name, path, new_path)` | 移动或重命名文件；目标父目录须存在，不覆盖已有目标 |
| `move_folder(skill_name, path, new_path)` | 移动或重命名目录；不能移入自身子目录 |
| `delete_file(skill_name, path)` | 删除普通文件 |
| `delete_folder(skill_name, path)` | 递归删除目录及内容 |

除查询和下载外，上述方法成功时返回 `None`。

```python
client.create_folder("user_001", "personal-notes", "references")
client.create_file("user_001", "personal-notes", "references/example.txt")
client.upload_file(
    "user_001", "personal-notes", "references/example.txt",
    file_bytes="笔记示例".encode("utf-8"),
)
content = client.download_file("user_001", "personal-notes", "references/example.txt")
client.move_file("user_001", "personal-notes", "references/example.txt", "references/renamed.txt")
client.move_folder("user_001", "personal-notes", "references", "examples")
tree = client.get_resource_tree("user_001", "personal-notes")
client.delete_file("user_001", "personal-notes", "examples/renamed.txt")
client.delete_folder("user_001", "personal-notes", "examples")
```

公共调用示例：`public.get_resource_tree(skill_name="shared-summary")`。

资源节点包含 `name`、`path`、`type`、`content_type`、`children`。`type` 为 `file` 或 `folder`；目录的 `content_type` 为 `None`，文件按 MIME 类型识别，未知类型为 `application/octet-stream`。文件和空目录的 `children` 为 `None`。资源树将目录排在前面，按名称拼音排序，隐藏点号目录且不递归符号链接。

## 参数约束与注意事项

- 技能名称去除首尾空白后须为 1～64 个小写字母、数字或连字符，不能以连字符开头或结尾，不能有连续连字符。`description` 必须非空，`extra_fields` 的值必须是字符串。
- 私有 `user_id` 必须非空，并满足存储层的用户 ID 规则。管理器不替宿主判断用户是否有权操作其他用户或公共技能。
- 资源路径使用 `/` 分隔，不接受绝对路径、`..`、反斜杠、NUL 或技能根目录。管理读写不通过符号链接进行；复制的包内也不能包含符号链接或特殊文件。
- `upload_file()` 只覆盖已经存在的 UTF-8 文本，不接受含 NUL 的内容。参数名为 `file_bytes`。
- 根目录 `SKILL.md` 只能通过上传完整文件更新，不能单独创建、移动或删除。其 YAML 头必须包含与目录一致的 `name` 和非空 `description`；删除整个技能使用 `delete_skill()`。
- 无效的技能文档可能被列表查询跳过；直接查询该技能详情会报错。接口失败时抛出异常，不返回错误字典。

常见异常包括：目标已存在的 `FileExistsError`、源文件缺失的 `FileNotFoundError`、路径类型不匹配的 `NotADirectoryError` / `IsADirectoryError`、参数或文档无效的 `ValueError` / `TypeError`，以及权限、编码和磁盘错误。

技能包写入会刷新技能视图并通知缓存。若出现 `RuntimeError("技能已修改，但共享技能缓存失效通知发布失败")`，文件可能已经提交，应先重新查询再决定是否重试。公共预设修改、私有开关修改及管理器成功发布的技能变更，会由已有客户端和池在下一轮自动识别，无需手动调用 `pool.invalidate_agents()`。直接编辑技能包内容后，应发布技能刷新通知或按 [Agent 管理器说明](AGENT_AGENTS.md#状态缓存与关闭) 刷新池。
