# 环境变量管理器

`DeerFlowEnvironmentClient` 与 `DeerFlowClientPool`、`DeerFlowSkillClient`、
`DeerFlowThreadClient` 同级，不创建 Agent、调用模型或主动启动沙盒。
配置注入遵循同一约定：显式 `config_path` 优先，否则使用 `DEERFLOW_CONFIG_PATH`。

```python
from deerflow import DeerFlowEnvironmentClient

environment = DeerFlowEnvironmentClient(config_path="config.yaml")

# 公共变量不依赖用户身份。
environment.public.create_variable("SERVICE_URL", "https://service.example")
environment.public.update_variable("SERVICE_URL", "https://new-service.example")
environment.public.get_variable("SERVICE_URL")
environment.public.list_variables()

# 私有接口必须显式指定存储 user_id，与线程管理器使用相同的用户目录标识。
environment.create_variable("alice", "SERVICE_TOKEN", "user-token")
environment.update_variable("alice", "SERVICE_TOKEN", "new-user-token")
environment.get_variable("alice", "SERVICE_TOKEN")
environment.list_variables("alice")
environment.get_effective_variables("alice")

environment.delete_variable("alice", "SERVICE_TOKEN")
environment.public.delete_variable("SERVICE_URL")
```

`list_variables()` 返回 `{变量名: 字符串值}`，`get_variable()` 返回字符串；
创建、更新、删除成功时返回 `None`。重复创建抛出 `ValueError`，读取、更新、
删除不存在的名称抛出 `KeyError`。管理器是可信宿主接口，调用方负责管理操作鉴权。
返回值包含明文，调用方不应把管理接口返回值直接写入日志或模型上下文。

## 文件与冲突规则

公共变量保存在 `{DEER_FLOW_HOME}/.bashrc`，私有变量保存在
`{DEER_FLOW_HOME}/users/{user_id}/.bashrc`；未配置数据根目录时沿用项目的
`.deer-flow`。这里的 `user_id` 为合法的存储标识，只含字母、数字、下划线或连字符。
公共增删改查均不接受 `user_id`。

变量名遵循 `[A-Za-z_][A-Za-z0-9_]*`，值必须是无 NUL 的字符串。
私有变量不能与公共变量重名；公共新增也会检查所有已有私有文件，防止反向冲突。
不同用户可以有同名私有变量。修改只更新值，重命名可通过删除后创建完成。
读入手工造成的公共／私有重名时拒绝执行命令，不静默覆盖其中一份值。

文件由管理器维护为经过 shell 引用的 `export NAME=VALUE` 赋值集合，权限为
`0600`。空格、引号、多行和 `$()` 等内容都是字面值，既不展开宿主环境变量，
也不运行 shell 代码。不支持将任意个人 shell 初始化脚本放进这些文件；格式损坏
时保留原文件并报错。允许手工修改符合这一格式的文件，下次读取即可看到修改。

公共及私有读写共用线程锁和跨进程文件锁，完整覆盖冲突检查、读取和修改；
写入通过同目录临时文件加原子替换完成。多实例需要共享同一个数据目录及有效的
文件锁语义，使用不同磁盘的部署不会自动同步。拒绝变量路径中的符号链接。

## 注入与复用

框架在懒加载、预先获取及复用沙盒时绑定可信用户身份。每次执行命令时重新读取
公共与当前用户的 `.bashrc`，逻辑合并后通过结构化 `env` 注入命令及其子进程。
不需要把两份文件复制进容器或执行 `source ~/.bashrc`；变量不会写入宿主进程的
`os.environ`、工具参数或 checkpoint。两个用户的私有值不会混用。

优先级为：本次调用的显式环境／技能凭据 > 管理器变量 > 提供方已有默认环境。
DeerFlow 的可信身份变量仍由框架设置。删除管理器中的变量后停止注入该值；
如果提供方或操作系统本来就有同名默认变量，它会恢复为默认值。

变量不缓存到沙盒容器的初始环境中，因此十分钟保留期间无需重建容器：
一次管理修改成功后，后续开始的命令读取最新值。已开始执行的命令和其后台进程
继续使用启动时的环境，若需要更新这些进程，应重新启动相应进程。

Local、AIO、E2B、BoxLite、Tenki 和 OpenSandbox 的命令入口均接入同一刷新逻辑。
主代理、子代理以及同步／异步 `bash` 工具共享这一行为。第三方提供方通过
`bash` 工具也会收到合并后的 `env`；若直接调用自定义 `Sandbox.execute_command`，
实现应先调用 `self.command_environment(env)`。

AIO 有托管变量时沿用现有临时 `bash.exec(env=...)` 会话，避免删除后的值残留在
持久 shell 中。此模式下跨命令的 `cd`、`export`、激活虚拟环境等 shell 状态不继承，
相关步骤应放在同一条命令里。没有变量且没有请求凭据时仍使用原有持久会话。
镜像需要支持项目已有的 `bash.exec` 环境注入接口，不支持时沿用提供方的明确错误。

`bash` 输出沿用凭据名称识别规则进行脱敏；普通配置值可正常读取。
文件工具本身没有进程环境；通过 `bash` 运行的脚本及其子进程可直接读取变量。
宿主 MCP 服务、宿主 ACP 进程或其他独立外部服务不属于沙盒变量注入范围。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_environment_client.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_sandbox_environment.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_management_client_config.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_sandbox_async_arguments.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_package_imports.py' -v
```

本地 shell 测试验证真实进程继承；远程测试使用实际适配器与离线传输替身验证
更新、删除、作用域及结构化 env 参数，不依赖 Docker 或云服务。
