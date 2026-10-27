# 当前配置工具输入输出实测

测试日期：2026-10-27（Asia/Shanghai）。原始全量实测代码基线：`0a5a6d4`；`web_search` 输出格式已按当前工作区修改更新，补充验证范围见该节。

原始全量实测使用根目录 `config.yaml` 中的真实模型 `doubao-seed-2-1-turbo-260628`，由 `deerflow.models.patched_deepseek:PatchedChatDeepSeek` 调用，`thinking_enabled=true`。该轮实测参数来自模型生成的 tool_calls，使用 `DeerFlowClient.stream()` 和现有中间件执行；输出优先取下一次模型调用中的 ToolMessage.content，而非模型最后总结。没有使用 mock 返回。

显式配置工具 10 个，框架在当前嵌入式客户端默认模式下自动加入 5 个，共 15 个。沙盒为 `LocalSandboxProvider`，`allow_host_bash=true`。运行数据放在独立临时 DEER_FLOW_HOME 中，使用专门测试用户和独立线程；测试文件由宿主预置。没有验证 Docker 沙盒。

“输入”仅指模型可见、需要生成的参数，不包含 runtime、tool_call_id 等框架注入字段。输入代码块中的 `//` 是文档注释，实际传输 JSON 不包含注释。参数表列出模型未显式传入的可选参数及默认值。纯文本输出以 JSON 字符串表示以保留换行；JSON 字符串结果会明确标注并展开展示。

当前中间件可能转义远程内容中的控制标签，并将长工具输出保存到文件后改成摘要与文件引用（tool_output.enabled=true，externalize_min_chars=12000）。因此不能承诺每次模型都会收到工具函数的完整原始 JSON。read_file 被此层豁免，但仍有自身 50000 字符限制。artifact、状态元数据与客户端流事件不是这里的普通工具正文。

## web_search

根据关键词检索网页、文档、学术资料、图片、视频或播客。当前实现为 Metaso。

输入

```json
{
  "query": "Python official documentation", // 查询语句或检索关键词
  "scope": "webpage" // 检索类型：webpage / document / scholar / image / video / podcast
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `query` | 必填 | 查询语句或检索关键词 |
| `scope` | 可选；"webpage" | 检索类型：webpage / document / scholar / image / video / podcast |

输出

情况 1：成功返回仅含 `title`、`url`、`snippet` 的结果数组。

实际为 JSON 字符串，不增加外层包装。以下示例沿用原始实测内容，按当前三个字段的格式整理，仅展示首条结果；长文本已标注节选。

```json
[
  {
    "title": "How to Learn Python for Free: 20 Best Online Resources of 2025",
    "url": "https://mimo.org/blog/how-to-learn-python-for-free-online",
    "snippet": "6. The official Python tutorial\n- Best for: Self-learners who want comprehensive, technical documentation\nThe official Python documentation offers a thorough, text-based tutorial c…（文档节选）"
  }
]
```

情况 2：异常返回以 `Error: ` 开头的字符串。以下为已实测的无效 scope 示例。

输入

```json
{
  "query": "test",
  "scope": "invalid"
}
```

输出

```json
"Error: Unsupported Metaso search scope"
```

仅 query 必填，scope 默认 webpage。当前配置 max_results=10，三个 include/concise 开关均为 false。每项仅包含 title、url、snippet 三个字符串字段；url 依次取上游响应中的 url、link、imageUrl，缺失或空值归一化为空字符串。link、score、position、日期、作者、图片尺寸、时长等原始元数据不进入输出。空结果为 JSON 字符串 `[]`。

原始全量实测通过真实模型调用了六种 scope。格式更新后，使用 `.env` 和 `config.yaml` 中的真实配置，直接通过 `web_search_tool.ainvoke()` 逐项调用六种 scope；query 均为 `artificial intelligence`，仅在验证进程中将 max_results 设为 1。下表为更新后的真实 API 验证结果；本次未重跑模型与中间件链路。

| scope | 更新后验证条数 | 输出字段 |
|---|---:|---|
| `webpage` | 1 | `title`, `url`, `snippet` |
| `document` | 1 | `title`, `url`, `snippet` |
| `scholar` | 1 | `title`, `url`, `snippet` |
| `image` | 1 | `title`, `url`, `snippet` |
| `video` | 1 | `title`, `url`, `snippet` |
| `podcast` | 1 | `title`, `url`, `snippet` |

12 项离线测试通过，覆盖六种结果结构、字段过滤及 HTTP、网络、格式、配置等错误分支；更新后也验证了无效 scope 返回固定的 `Error: ` 前缀。

`scope=image` 的首条结果示例：

以下沿用原始实测内容，按当前格式展开 JSON 字符串中的首条结果。

```json
[
  {
    "title": "ai大脑图片素材-正版创意图片500513236-摄图网",
    "url": "https://imgs.699pic.com/images/500/513/236.jpg!detail.v1",
    "snippet": ""
  }
]
```

`scope=video` 的首条结果示例：

以下沿用原始实测内容，按当前格式展开 JSON 字符串中的首条结果。

```json
[
  {
    "title": "人工智能的定义与发展",
    "url": "http://www.bilibili.com/video/av41695308?t=0",
    "snippet": "人工智能（AI）是计算机科学的一个分支，致力于让机器展现人类智能。它起源于1956年达特茅斯大学的命名，经历了从学术实验室到技术爆发的演进过程。当前AI主要分为两大类：基于规则的传统机器人（需明确编程指令）和机器学习（通过数据分析自我提升）。AI正深刻影响各行各业，帮助人类更高效地完成任务，而非取代人类工作。"
  }
]
```

`scope=podcast` 的首条结果示例：

以下沿用原始实测内容，按当前格式展开 JSON 字符串中的首条结果。

```json
[
  {
    "title": "人工智能",
    "url": "https://www.xiaoyuzhoufm.com/episode/67405d7f11045e78e5197b1b",
    "snippet": ""
  }
]
```

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
"Error: Metaso request failed (HTTP 429)"
```

```json
"Error: Metaso request timed out"
```

```json
"Error: Metaso returned an unexpected response format"
```


## web_fetch

获取指定公开网页的正文。当前实现为 Metaso Reader。

输入

```json
{
  "url": "https://example.com" // 完整网页 URL，必须带协议；使用用户提供或搜索结果中的地址
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `url` | 必填 | 完整网页 URL，必须带协议；使用用户提供或搜索结果中的地址 |

输出

情况 1：本次真实调用，模型实际收到的结果。

以下节选正文首段，其余多语言正文略；未改写该段文本。

```json
"This domain is for use in documentation examples without needing permission. This is not a service; avoid relying on it for testing and monitoring purposes."
```

成功结果是正文字符串，可能包含 Markdown 或 HTML；不是固定的 {title, content} 对象。受登录限制的私有页面不在此工具能力范围内。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
"Error: Metaso request failed (HTTP 429)"
```

```json
"Error: Metaso request timed out"
```

```json
"Error: Metaso returned an unexpected response format"
```


## image_search

根据关键词检索图片，返回原图、缩略图和来源页面地址。当前实现为 DuckDuckGo。

输入

```json
{
  "query": "red apple", // 查询语句或检索关键词
  "max_results": 2 // 最多返回的结果数量；注意部分工具会被配置中的数量覆盖
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `query` | 必填 | 查询语句或检索关键词 |
| `max_results` | 可选；5 | 最多返回的结果数量；注意部分工具会被配置中的数量覆盖 |
| `size` | 可选；null | 图片尺寸：Small / Medium / Large / Wallpaper |
| `color` | 可选；null | 颜色筛选，例如 color、Monochrome、Red、Blue；见完整选项说明 |
| `type_image` | 可选；null | 图片类型：photo / clipart / gif / transparent / line |
| `layout` | 可选；null | 布局：Square / Tall / Wide |
| `license_image` | 可选；null | 许可：any / Public / Share / ShareCommercially / Modify / ModifyCommercially |

输出

情况 1：本次真实调用，模型实际收到的结果。

实际为 JSON 字符串，下方展开其内容。

```json
{
  "query": "red apple",
  "total_results": 5,
  "results": [
    {
      "title": "Fresh Red Delicious Apple | Fig App",
      "image_url": "https://i5.walmartimages.com/seo/Fresh-Red-Delicious-Apple-Each_7320e63a-de46-4a16-9b8c-526e15219a12_3.e557c1ad9973e1f76f512b34950243a3.jpeg",
      "thumbnail_url": "https://ts4.mm.bing.net/th?id=OIP.owUjUdRhtPxKPXev6aIAuwHaHa&pid=15.1",
      "source_url": "https://foodisgood.com/product/fresh-red-delicious-apple/"
    },
    {
      "title": "Fresh red apple fruit isolated on the white background with clipping ...",
      "image_url": "https://as1.ftcdn.net/v2/jpg/02/10/59/70/1000_F_210597040_wxyvT8wGl322pfG6xO4LGW4FCWTgkOyX.jpg",
      "thumbnail_url": "https://ts2.mm.bing.net/th?id=OIP.dfXQyVXEmomPKY8sbq9k4AHaG7&pid=15.1",
      "source_url": "https://stock.adobe.com/images/fresh-red-apple-fruit-isolated-on-the-white-background-with-clipping-path-one-of-the-best-isolated-apples-that-you-have-seen/210597040"
    },
    {
      "title": "Red Apple Fruit Wallpapers - Wallpaper Cave",
      "image_url": "https://wallpapercave.com/wp/wp2723086.jpg",
      "thumbnail_url": "https://ts4.mm.bing.net/th?id=OIP.hQittHjUXb6SZLIEtlsl2QHaHa&pid=15.1",
      "source_url": "https://wallpapercave.com/red-apple-fruit-wallpaper"
    },
    {
      "title": "Red Apple Fruit",
      "image_url": "https://www.applesfromny.com/wp-content/uploads/2020/06/Red-DeliciousNEW.png",
      "thumbnail_url": "https://ts3.mm.bing.net/th?id=OIP.3yVF0tT4qKy05GH7wq_BDwHaF1&pid=15.1",
      "source_url": "https://fity.club/lists/suggestions/red-apple-fruit/"
    },
    {
      "title": "Red Apple Anchorage Hours at Carlos Snyder blog",
      "image_url": "https://png.pngtree.com/png-clipart/20230930/original/pngtree-small-dark-red-apple-png-image_13024127.png",
      "thumbnail_url": "https://ts2.mm.bing.net/th?id=OIP.cAX4nzjxEQt6S2Iymxbh-gHaHa&pid=15.1",
      "source_url": "https://storage.googleapis.com/daezepfdutguoe/red-apple-anchorage-hours.html"
    }
  ],
  "usage_hint": "Use the 'image_url' values as reference images in image generation. Download them first if needed."
}
```

当前 config.yaml 的 max_results=5 覆盖模型传入的 max_results，本次即使模型请求 2 条也按配置检索。color 的可选值为 color、Monochrome、Red、Orange、Yellow、Green、Blue、Purple、Pink、Brown、Black、Gray、Teal、White。工具返回错误对象时不以 Error: 开头。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
{
  "error": "No images found",
  "query": "xxx"
}
```

```json
{
  "error": "No safe image URLs found",
  "query": "xxx"
}
```

上述错误对象同样通过 JSON 字符串返回；底层依赖缺失或搜索异常也可能被归并为 No images found，不能仅凭该正文区分“确实无图片”和“服务暂不可用”。


## ls

列出目录及其下最多两层内容。当前宿主机实现返回逐行绝对路径。

输入

```json
{
  "path": "/mnt/user-data/workspace" // 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `path` | 必填 | 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"/mnt/user-data/workspace/nested/\n/mnt/user-data/workspace/nested/notes.md\n/mnt/user-data/workspace/red.png\n/mnt/user-data/workspace/sample.txt"
```

输出不是带 size/type 等属性的文件对象数组。路径显示的是线程虚拟路径，当前 ls_output_max_chars=20000；超限可能截断。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
"Error: Directory not found: /mnt/user-data/workspace/missing"
```


## read_file

读取 UTF-8 文本文件，可按行号读取指定范围。

输入

```json
{
  "path": "/mnt/user-data/workspace/sample.txt", // 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径
  "start_line": 2, // 起始行号，从 1 开始，包含该行
  "end_line": 3 // 结束行号，包含该行
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `path` | 必填 | 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |
| `start_line` | 可选；null | 起始行号，从 1 开始，包含该行 |
| `end_line` | 可选；null | 结束行号，包含该行 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"beta=2\nalpha=3"
```

情况 2：补充实测 `read_file_missing`。

输入

```json
{
  "path": "/mnt/user-data/workspace/not_exists.txt",
  "description": "测试读取不存在的文件，验证错误输出"
}
```

输出

```json
"Error: File not found: /mnt/user-data/workspace/not_exists.txt"
```

行号只控制切片，返回正文不会自动添加行号。未指定行范围时读取全文。当前 read_file_output_max_chars=50000；达到上限会附截断/继续读取提示。空文件、越界和无效行号有各自提示，不能都按空字符串处理。


## glob

在指定目录下按 glob 模式查找文件或目录。

输入

```json
{
  "path": "/mnt/user-data/workspace", // 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径
  "pattern": "**/*.md", // glob 模式或 grep 搜索表达式
  "include_dirs": false, // 是否包含匹配的目录
  "max_results": 10 // 最多返回的结果数量；注意部分工具会被配置中的数量覆盖
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `pattern` | 必填 | glob 模式或 grep 搜索表达式 |
| `path` | 必填 | 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |
| `include_dirs` | 可选；false | 是否包含匹配的目录 |
| `max_results` | 可选；200 | 最多返回的结果数量；注意部分工具会被配置中的数量覆盖 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"Found 1 paths under /mnt/user-data/workspace\n1. /mnt/user-data/workspace/nested/notes.md"
```

情况 2：补充实测 `glob_empty`。

输入

```json
{
  "pattern": "**/*.absent",
  "path": "/mnt/user-data/workspace"
}
```

输出

```json
"No files matched under /mnt/user-data/workspace"
```

输出是带数量与编号的文本。max_results 默认 200；实际取参数值与工具配置值的较小者（并执行合法范围约束）。超过上限有截断提示。


## grep

在指定文件或目录中查找匹配的文本行。

输入

```json
{
  "pattern": "alpha", // glob 模式或 grep 搜索表达式
  "path": "/mnt/user-data/workspace", // 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径
  "glob": "**/*.txt", // 过滤待搜索文件的 glob 模式
  "literal": true, // 是否将 pattern 作为普通字符串而非正则表达式
  "case_sensitive": true, // 是否区分大小写
  "max_results": 10 // 最多返回的结果数量；注意部分工具会被配置中的数量覆盖
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `pattern` | 必填 | glob 模式或 grep 搜索表达式 |
| `path` | 必填 | 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |
| `glob` | 可选；null | 过滤待搜索文件的 glob 模式 |
| `literal` | 可选；false | 是否将 pattern 作为普通字符串而非正则表达式 |
| `case_sensitive` | 可选；false | 是否区分大小写 |
| `max_results` | 可选；100 | 最多返回的结果数量；注意部分工具会被配置中的数量覆盖 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"Found 2 matches under /mnt/user-data/workspace\n/mnt/user-data/workspace/sample.txt:1: alpha=1\n/mnt/user-data/workspace/sample.txt:3: alpha=3"
```

情况 2：补充实测 `grep_empty`。

输入

```json
{
  "pattern": "NO_SUCH_TOKEN",
  "path": "/mnt/user-data/workspace/sample.txt"
}
```

输出

```json
"No matches found under /mnt/user-data/workspace/sample.txt"
```

输出是带数量和 path:line: content 的文本。max_results 默认 100；实际取参数值与工具配置值的较小者（并执行合法范围约束）。无匹配与路径不存在是不同结果。


## write_file

新建、覆盖或追加文本文件。

输入

```json
{
  "path": "/mnt/user-data/workspace/created.txt", // 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径
  "content": "hello tool\n", // 写入的文本内容
  "append": false // true 追加，false 覆盖或新建
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `path` | 必填 | 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径 |
| `content` | 必填 | 写入的文本内容 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |
| `append` | 可选；false | true 追加，false 覆盖或新建 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"OK"
```

成功返回字符串 OK。当前 read_before_write.enabled=true：覆盖或追加已有文件前，必须先 read_file 读取当前版本；写入后旧读取凭据失效，需要重新读取。新建文件无需预读。非追加单次写入默认受 80 KiB UTF-8 内容上限约束。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
"Error: Permission denied writing to file: xxx"
```


## str_replace

替换文本文件中的指定字符串。

输入

```json
{
  "path": "/mnt/user-data/workspace/sample.txt", // 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径
  "old_str": "beta=2", // 要替换的原字符串
  "new_str": "beta=20", // 替换后的字符串
  "replace_all": false, // 是否替换所有匹配；false 时当前实现只替换首个匹配
  "description": "将 beta=2 替换为 beta=20" // 调用目的说明，通常展示在界面上
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `path` | 必填 | 绝对路径；本次使用 /mnt/user-data 下的线程虚拟路径 |
| `old_str` | 必填 | 要替换的原字符串 |
| `new_str` | 必填 | 替换后的字符串 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |
| `replace_all` | 可选；false | 是否替换所有匹配；false 时当前实现只替换首个匹配 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"OK"
```

情况 2：补充实测 `str_replace_missing`。

输入

```json
{
  "path": "/mnt/user-data/workspace/sample.txt",
  "old_str": "NOT_FOUND",
  "new_str": "x"
}
```

输出

```json
"Error: String to replace not found in file: /mnt/user-data/workspace/sample.txt"
```

成功返回字符串 OK。修改前必须先 read_file 读取当前版本。注意工具描述宣称 replace_all=false 时原串必须唯一，但当前函数实现直接替换首个匹配；不要把“重复匹配必报错”写成已验证保证。


## bash

在当前配置的宿主机环境执行 shell 命令，返回命令输出。

输入

```json
{
  "command": "printf 'tool_io_ok\\n'", // 要执行的 shell 命令
  "description": "执行指定的 printf 命令用于工具输入输出实测" // 调用目的说明，通常展示在界面上
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `command` | 必填 | 要执行的 shell 命令 |
| `description` | 可选；"" | 调用目的说明，通常展示在界面上 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"tool_io_ok\n"
```

情况 2：补充实测 `bash_error`。

输入

```json
{
  "command": "printf 'test_stderr\\n' >&2; exit 7",
  "description": "测试非零退出码与 stderr 输出"
}
```

输出

```json
"test_stderr\n\nExit Code: 7"
```

当前 LocalSandboxProvider 与 allow_host_bash=true，命令在宿主机执行；默认工作目录映射到当前线程 workspace。bash_command_timeout=600 秒，bash_output_max_chars=20000。输出是字符串，不是 {stdout, stderr, exit_code} 对象；非零退出详情以本次实际输出为准。


本次退出码 7 的返回文本不以 `Error: ` 开头，工具状态元数据仍标记为 success；判断命令是否成功时应读取正文中的退出信息。正常无输出时返回 `(no output)`；超时分支会附退出码 124（后两项为源码补充，本次未触发）。

## present_files

将当前线程 outputs 目录中的文件登记为向用户展示的产物。框架自动加入。

输入

```json
{
  "filepaths": ["/mnt/user-data/outputs/report.txt"] // 需要展示的文件路径数组，限定 /mnt/user-data/outputs 下
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `filepaths` | 必填 | 需要展示的文件路径数组，限定 /mnt/user-data/outputs 下 |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"Successfully presented files"
```

模型只提供 filepaths；runtime 和 tool_call_id 由框架注入。工具内部返回 Command，同时更新 artifacts 与消息，但模型收到的是 ToolMessage.content 文本。该工具主要登记展示路径，不应将成功回执理解为文件内容或文件有效性的完整校验。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
"Error: Only files in /mnt/user-data/outputs can be presented: xxx"
```


## ask_clarification

向用户提出澄清问题、选项或表单，并结束当前轮次等待用户回复。框架自动加入。

输入

```json
{
  "question": "报告需要使用中文还是英文？", // 向用户提出的问题
  "clarification_type": "approach_choice", // missing_info / ambiguous_requirement / approach_choice / risk_confirmation / suggestion
  "options": ["中文", "英文"] // 单选候选项；复杂或多选交互使用 fields
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `question` | 必填 | 向用户提出的问题 |
| `clarification_type` | 必填 | missing_info / ambiguous_requirement / approach_choice / risk_confirmation / suggestion |
| `context` | 可选；null | 补充说明为什么需要澄清 |
| `options` | 可选；null | 单选候选项；复杂或多选交互使用 fields |
| `fields` | 可选；null | 表单字段数组；提供时优先于 options，详见下文 |

输出

此工具由 ClarificationMiddleware 拦截，正常流程不会把函数占位返回值 "Clarification request processed by middleware" 当作模型结果。它是 return_direct 工具，本轮通常不再发起后续模型请求；用户答复通过之后的用户消息进入下一轮。

本轮没有工具执行后的再次模型调用，因此不存在“模型在本轮接收普通返回值”的样本。实际落入消息历史并送往客户端的澄清内容如下：

客户端事件节选，省略状态推导和回执元数据：

```json
{
  "type": "tool_result",
  "agent_id": "main",
  "data": {
    "id": "clarification:call_gny0wy0ntisfyr1c98g3369i",
    "content": "🔀 报告需要使用中文还是英文？\n\n  1. 中文\n  2. 英文",
    "tool_call_id": "call_gny0wy0ntisfyr1c98g3369i",
    "name": "ask_clarification",
    "status": "success",
    "artifact": {
      "human_input": {
        "version": 1,
        "kind": "human_input_request",
        "source": "ask_clarification",
        "request_id": "clarification:call_gny0wy0ntisfyr1c98g3369i",
        "clarification_type": "approach_choice",
        "question": "报告需要使用中文还是英文？",
        "input_mode": "choice_with_other",
        "tool_call_id": "call_gny0wy0ntisfyr1c98g3369i",
        "options": [
          {
            "id": "option-1",
            "label": "中文",
            "value": "中文"
          },
          {
            "id": "option-2",
            "label": "英文",
            "value": "英文"
          }
        ]
      }
    }
  },
  "thread_id": "tool-io-ask-clarification",
  "seq": 65
}
```

`fields` 的模型输入形态（源码补充；本次实测使用 options 单选）：

```json
[
  {
    "name": "language",
    "label": "报告语言",
    "type": "select",
    "required": true,
    "options": [
      "中文",
      "英文"
    ],
    "placeholder": "请选择"
  }
]
```

每个字段的 name 必填且唯一；type 支持 text、textarea、number、select、multi_select、checkbox、date。label 默认等于 name，type 默认 text，required 默认 false；select/multi_select 需要 options。上限为 16 个字段、每字段 24 个选项，name/label/option/placeholder 各不超过 200 字符；超限会退化为普通文本提问。checkbox 表示布尔值，required=true 表示必须勾选。


## review_skill_package

静态检查技能包，不安装、激活或执行被检查的技能。框架自动加入。

输入

```json
{
  "target": "inline://SKILL.md", // 技能目标，例如 inline://SKILL.md、skill://… 或允许目录内的本地路径
  "profile": "deerflow", // 校验规范：deerflow / agentskills
  "include_content": "facts-only", // 附带内容范围：none / facts-only / semantic-review
  "scope": ["all"], // 审查维度数组，省略或为空时使用 ["all"]
  "inline_content": "---\nname: io-test\ndescription: 测试用文本整理技能\n---\n# 文本整理\n将用户提供的文本整理为三条要点。" // 内联 SKILL.md 内容；target 为 inline:// 时必填
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `target` | 必填 | 技能目标，例如 inline://SKILL.md、skill://… 或允许目录内的本地路径 |
| `profile` | 可选；"deerflow" | 校验规范：deerflow / agentskills |
| `include_content` | 可选；"semantic-review" | 附带内容范围：none / facts-only / semantic-review |
| `scope` | 可选；null | 审查维度数组，省略或为空时使用 ["all"] |
| `inline_content` | 可选；null | 内联 SKILL.md 内容；target 为 inline:// 时必填 |

输出

情况 1：本次真实调用，模型实际收到的结果。

实际为 JSON 字符串，下方展开其内容。

```json
{
  "artifacts": [],
  "facts": {
    "analyzer_errors": [],
    "completeness": {
      "not_assessed": [],
      "package_enumerated": true,
      "text_content_complete": true,
      "truncated": false
    },
    "evals": {
      "case_count": 0,
      "manifests": [],
      "negative_trigger_cases": 0,
      "positive_trigger_cases": 0,
      "schema": null,
      "valid": null
    },
    "findings": [],
    "profile": "deerflow",
    "reader_errors": [],
    "resources": {
      "edges": [],
      "nodes": [
        {
          "kind": "text",
          "path": "SKILL.md"
        }
      ],
      "orphans": []
    },
    "schema_version": "deerflow.skill-review.facts.v1",
    "subject": {
      "category": null,
      "declared_name": "io-test",
      "display_ref": "inline://SKILL.md",
      "package_digest": "sha256:ae068e0fa21025793e07b76448d1f07d56a3ceb5a90e3488e416c39bfd3a74e3",
      "source": "inline"
    },
    "summary": {
      "blockers": 0,
      "errors": 0,
      "infos": 0,
      "warnings": 0
    }
  },
  "static_report": {
    "assurance": "static_only",
    "dimensions": [
      {
        "id": "structure",
        "status": "pass",
        "summary": "0 blocker(s), 0 error(s), 0 warning(s)"
      },
      {
        "id": "evidence_quality",
        "status": "concern",
        "summary": "0 eval case(s) detected"
      }
    ],
    "evidence": {
      "baseline": null,
      "facts_complete": true,
      "limitations": [],
      "retained_artifacts": [],
      "runtime_runs": []
    },
    "issues": [],
    "readiness": "publish_candidate",
    "recommended_actions": [],
    "review": {
      "completed_at": "2026-10-27T03:14:38Z",
      "facts_schema_version": "deerflow.skill-review.facts.v1",
      "profile": "deerflow",
      "reviewer_model": "deterministic-review-core",
      "scope": [
        "all"
      ]
    },
    "schema_version": "deerflow.skill-review.report.v1",
    "subject": {
      "display_ref": "inline://SKILL.md",
      "package_digest": "sha256:ae068e0fa21025793e07b76448d1f07d56a3ceb5a90e3488e416c39bfd3a74e3"
    }
  },
  "untrusted_review_data": true
}
```

模型看到的是审查摘要，不是 Python Command 对象，也不是完整 artifact。完整 facts、artifacts、static_report、markdown 等数据可在原生 ToolMessage.artifact 中保留；是否进入模型上下文要以 content 为准。include_content 控制审查内容范围，目标文件属于不可信数据。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
"Error: failed to review skill package: ValueError: inline_content is required for inline:// targets"
```

本次 content 顶层字段为 untrusted_review_data、facts、artifacts、static_report；完整 artifact 额外带有 markdown。static_report.assurance=static_only 表示仅做静态检查，不代表执行过技能。


## list_uploaded_files

发现当前线程之前上传的文件，可过滤和分页。本轮新上传的文件不包含在结果中；嵌入式客户端默认加入。

输入

```json
{
  "include_outline": true, // false 不附大纲；true 为可解析文件附大纲；也可以传指定文件名数组
  "max_results": 5, // 每页数量，最多 100 条
  "extensions": ["txt"] // 扩展名过滤数组，可带点或不带点，忽略大小写，与 query 取交集
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `include_outline` | 可选；false | false 不附大纲；true 为可解析文件附大纲；也可以传指定文件名数组 |
| `max_results` | 可选；20 | 每页数量，最多 100 条 |
| `query` | 可选；null | 文件名的大小写不敏感子串 |
| `extensions` | 可选；null | 扩展名过滤数组，可带点或不带点，忽略大小写，与 query 取交集 |
| `cursor` | 可选；null | 上一页 next_cursor；续页时保持 query 和 extensions 不变 |

输出

情况 1：本次真实调用，模型实际收到的结果。

实际为 JSON 字符串，下方展开其内容。

```json
{
  "files": [
    {
      "filename": "history.txt",
      "size": 25,
      "path": "/mnt/user-data/uploads/history.txt",
      "extension": ".txt"
    }
  ],
  "total_count": 1,
  "message": "Found 1 historical file(s)."
}
```

工具函数返回 dict，进入模型上下文后是 JSON 字符串。files 中的 size 为字节数，extension 带点；outline 与 outline_preview 仅在可提取时出现。存在下一页时返回 truncated=true、omitted_summary、next_cursor，最后一页不包含 next_cursor。无运行上下文、无目录、空结果等通常返回 files=[] 和 message；游标错误使用结构化错误对象。

其他返回形态（源码补充，以下并非本次真实失败记录；xxx 为占位值）：

```json
{
  "files": [],
  "message": "No historical uploaded files in this thread."
}
```


游标错误示例（源码补充；error 还可能为 invalid_cursor）：

```json
{
  "files": [],
  "error": "stale_cursor",
  "restart_required": true,
  "message": "Upload listing continuation is invalid or has changed. Restart without cursor using the desired filters; discard earlier pages before combining results."
}
```

## view_image

读取当前线程图片，并让模型在后续请求中接收图片内容。当前模型 supports_vision=true，框架自动加入。

输入

```json
{
  "image_path": "/mnt/user-data/workspace/red.png" // 图片绝对虚拟路径，限定 /mnt/user-data/workspace、uploads、outputs
}
```

| 参数 | 必填 / 默认值 | 含义 |
|---|---|---|
| `image_path` | 必填 | 图片绝对虚拟路径，限定 /mnt/user-data/workspace、uploads、outputs |

输出

情况 1：本次真实调用，模型实际收到的结果。

```json
"Successfully read image"
```

情况 2：补充实测 `view_image_missing`。

输入

```json
{
  "image_path": "/mnt/user-data/workspace/missing.png"
}
```

输出

```json
"Error: Image file not found: /mnt/user-data/workspace/missing.png"
```

模型不生成 runtime 或 tool_call_id。工具内部更新 viewed_images；模型先收到成功回执，随后 ViewImageMiddleware 注入包含 text/image_url 的 HumanMessage。模型真正能看见图片依赖后者，而不是仅凭成功回执。支持 jpg、jpeg、png、webp、gif，并校验文件头、大小及扩展名。

成功后的后续模型请求还实际收到以下 HumanMessage 内容块（Base64 已省略）：

```json
[
  {
    "type": "text",
    "text": "Here are the images you've viewed:"
  },
  {
    "type": "text",
    "text": "\n- **/mnt/user-data/workspace/red.png** (image/png)"
  },
  {
    "type": "image_url",
    "image_url": {
      "url": "data:image/png;base64,<图片字节的 Base64，文档省略>"
    }
  }
]
```

本次模型据此识别为红色图片。


## 测试范围与记录

| 工具 | 来源 | 实测结论 |
|---|---|---|
| `web_search` | config.yaml | 原始模型链路已验证；格式更新后另通过六种 scope 的真实工具调用和 12 项离线测试 |
| `web_fetch` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `image_search` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `ls` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `read_file` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `glob` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `grep` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `write_file` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `str_replace` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `bash` | config.yaml | 已验证模型生成参数、执行及模型接收结果 |
| `present_files` | 自动加入 | 已验证模型生成参数、执行及模型接收结果 |
| `ask_clarification` | 自动加入 | 已验证澄清交互并结束本轮 |
| `review_skill_package` | 自动加入 | 已验证模型生成参数、执行及模型接收结果 |
| `list_uploaded_files` | 自动加入 | 已验证模型生成参数、执行及模型接收结果 |
| `view_image` | 自动加入 | 已验证模型生成参数、执行及模型接收结果 |

原始全量实测覆盖 15 个工具，另补测 5 种搜索 scope 以及 7 个错误/空结果场景，共 27 个场景；`web_search` 格式更新后的补充验证单独记录在该节。没有工具因暂不可用而跳过。每个普通工具均保留了模型生成的调用和后续模型请求中的结果；ask_clarification 按其结束本轮的语义验证客户端事件。

以下不属于当前默认运行模式的启用工具：`task` 需要 subagent_enabled=true，`write_todos` 需要计划模式；`skill_manage`、任务连续性、调度、MCP 后台任务和批量子任务工具依赖对应开关或宿主能力。当前这些条件没有开启，因此未为实测而额外启用。`knowledge_base.enabled=false`；tool_groups 中出现 browser/knowledge 也不等于实际注册了工具。本次没有初始化额外 MCP 服务。

复核入口：`deerflow/tools/tools.py`（装配）、`deerflow/community/metaso/tools.py`、`deerflow/community/image_search/tools.py`、`deerflow/sandbox/tools.py`、`deerflow/tools/builtins/`，以及 `deerflow/agents/middlewares/` 下的澄清、图片和工具输出中间件。
