# deerflow-harness

从 [bytedance/deer-flow](https://github.com/bytedance/deer-flow) 提取的 DeerFlow Harness 独立模块仓库，保留模块提交历史，支持在二次开发后持续合并上游更新。

## 提取范围

用户需要的代码位于上游 `backend/packages/harness/deerflow/`。为保留 Python 包的正确结构，同时纳入它唯一的同级打包文件 `pyproject.toml`，固定拆分路径为 `backend/packages/harness`。

```text
deerflow-harness/
├── deerflow/                 # 上游框架代码，初始提取时保持原样
├── pyproject.toml            # 上游依赖声明及打包配置
├── LICENSE                  # 上游 MIT 许可证
├── UPSTREAM.json            # 固定拆分规则、初始提交及许可证摘要
├── scripts/sync_upstream.py  # 上游同步入口
└── tests/test_sync_upstream.py
```

此仓库是模块派生仓库，不属于 GitHub 原生 Fork 网络。前端、Gateway 应用、数据集和整个项目的测试集不在提取范围内。目录成为独立模块前的旧路径历史不会由 `git subtree split` 自动追踪。

## 克隆与开发

使用 SSH 443 端口，避免 VPN 环境下 GitHub 22 端口连接问题：

```bash
git -c core.sshCommand='ssh -o HostKeyAlias=github.com' clone \
  ssh://git@ssh.github.com:443/dearadi1/deerflow-harness.git
cd deerflow-harness
git config core.sshCommand 'ssh -o HostKeyAlias=github.com'
git switch -c feat/my_customization
```

需要已配置 GitHub SSH 密钥及可信的 GitHub 主机记录。这里只设置当前仓库，不修改全局 SSH 配置。

Python 包要求 Python 3.12 或更新版本。依赖由 `pyproject.toml` 管理，其中 `deerflow-extension-api` 是独立依赖；本仓库不复制该包。实际运行还需要按上游文档配置模型、沙箱、技能及运行目录。本次提取验证不代表完整运行环境已安装或业务功能已验证。

## 安装后的目录初始化

在通过 uv 安装了 `deerflow-harness` 的项目中运行：

```bash
uv run deerflow init
uv run deerflow init --target /path/to/project
uv run deerflow init --target /path/to/project --force
```

未指定 `--target` 时使用命令执行时的当前目录；目标目录不存在时自动创建。
命令将随包分发的 `.env.example` 和 `config.example.yaml` 分别复制为 `.env`、
`config.yaml`，并递归创建 `.deer-flow` 模板目录。当前包含
`agents/voris/config.yaml` 和 `agents/voris/SOUL.md`。本地密钥、数据库及用户运行数据不参与打包。

默认保留已有同名文件，并补齐缺失文件；`--force` 覆盖模板对应的同名文件，保留其他文件。
文件与目录类型冲突或模板目标包含符号链接时会报错，不继续复制。
输出会列出目标绝对路径及每个文件的创建、跳过或覆盖状态。
初始化不加载模型或 TUI，也不启动 DeerFlow。

生成后需要编辑 `.env` 和 `config.yaml`，配置模型及相关服务。
管理客户端默认仍从包所在目录查找配置，使用生成的配置时需显式传入
`config_path="/path/to/project/config.yaml"`；本命令不修改配置加载规则或环境变量。
若 uv 依赖来源固定了 Git revision，需要先更新到包含此功能的 revision 并同步依赖。

## 同步上游

### 交给 Agent 自动完成

在仓库目录内启动 Agent，直接发送：

> 请阅读并执行根目录 `SYNC_UPSTREAM_PROMPT.md`，将原版 DeerFlow 的最新 Harness 更新合并到本仓库，完成检查、测试、推送、PR 合并及本地同步，并报告结果。

完整指导见 [Agent 上游同步提示词](SYNC_UPSTREAM_PROMPT.md)。根目录 `AGENTS.md` 也已关联该流程，支持直接提出“同步原版 DeerFlow 更新”。Agent 会保留本地定制，通过 merge commit 合并；遇到需要业务决策或无法满足的合并条件时报告具体事项。

### 手动运行同步脚本

`chore/upstream_module` 永远只保存上游原版模块历史；定制代码通过功能分支和 PR 合入 `main`。

先确保当前分支是希望接收更新的最新代码，并且工作区干净：

```bash
git switch main
git pull --ff-only origin main
python3 scripts/sync_upstream.py
```

脚本使用 Python 标准库和 Git，需要可用的 `git subtree`。默认通过 HTTPS 443 获取公开上游，不需要 GitHub API Token。完整的提交及目录历史缓存在 `.git/upstream-source`，文件内容按需获取；缓存不会提交到模块仓库。首次拆分可能较慢，后续复用下载缓存，但仍会重新处理拆分历史。

脚本会验证许可证和历史连续性，更新本地 `chore/upstream_module`，再从当前分支创建 `chore/sync_upstream_<上游提交>` 并执行正常合并。只修改模块之外文件的上游更新不会产生同步提交。脚本不会推送远程或修改 `main`，完成后会打印推送命令。

如需通过 SSH 443 获取上游，首次创建缓存时指定：

```bash
GIT_SSH_COMMAND='ssh -o HostKeyAlias=github.com' python3 scripts/sync_upstream.py \
  --upstream-url ssh://git@ssh.github.com:443/bytedance/deer-flow.git
```

同一缓存后续应继续使用相同上游地址。可以通过 `--cache /绝对路径/专用缓存` 复用已有检出，脚本会验证地址是否匹配。

检查差异、运行相关测试，再推送脚本打印的同步分支及上游分支，通过 GitHub 创建 PR。**同步 PR 必须选择 Create a merge commit；不要 squash 或 rebase**，以保留已合并的上游祖先关系。上游源提交和拆分提交记录在同步合并提交的正文中；`UPSTREAM.json` 的 `initial_*` 字段只记录初始化来源。

发生冲突时，脚本保留同步分支和 Git 合并状态：解决后执行 `git add <已解决的文件>` 和 `git commit`，或执行 `git merge --abort`。重新尝试前可删除已放弃的同步分支，或直接在该分支手动合并 `chore/upstream_module`。上游强制改写历史、移动模块目录或修改许可证时，需要人工审查并调整策略。

## 验证同步工具

```bash
python3 -m unittest discover -s tests -p 'test_sync_upstream.py' -v
```

测试使用临时的本地 Git 仓库，不访问网络，也不启动 DeerFlow。覆盖定制保留、无关更新、重复同步、冲突、脏工作区和上游历史重写。

## 来源与许可

上游：[bytedance/deer-flow](https://github.com/bytedance/deer-flow)。保留原始作者、提交信息和 MIT 许可证，详见 `LICENSE`。新增同步工具与说明同样采用 MIT 许可证。上游代码中的原始注释及文档保持原样，新增维护代码使用中文注释和中文提交信息。
