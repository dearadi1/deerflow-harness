# Hours Knowledge 工具设计

本文是接口设计方案，尚未实现。对 Agent 开放三个工具：列出知识库、检索内容、下载文档。

知识库负责语义检索和提供转码后的 Markdown；Agent 下载后使用现有沙盒工具进行文本匹配、定位、按量读取和脚本处理。Markdown 保留页面信息、标题层级和可访问的图片 URL，不默认下载图片附件。

所有工具入参保持扁平，只使用字符串、数字、布尔值或字符串数组，不使用嵌套对象。

## 1. list_knowledge_bases

**用途：** 浏览当前允许访问的知识库，根据名称和介绍选择检索范围。

### 输入

```json
{
  "page": 1
}
```

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 否 | 页码，从 1 开始，默认 1 |

每页固定返回最多 20 个，不能修改。不提供关键词查询、每页数量或游标参数。

### 返回

```json
{
  "items": [
    {
      "dataset_id": "kb_product",
      "name": "产品技术资料",
      "description": "产品规格、使用说明及兼容性资料",
      "additional_kwargs": {}
    }
  ],
  "has_more": true,
  "total": 100
}
```

- `items`：本页知识库列表，示例仅展示一项。
- `additional_kwargs`：后端提供的扩展信息，原样透传，不限定内部字段，也不做结构解析。
- `has_more`：是否还有下一页；为 `true` 时可用 `page + 1` 继续查询。
- `total`：当前允许访问的知识库总数，不是本页数量。
- 按稳定顺序分页；超出最后一页时返回空 `items`、`has_more: false`，保留实际 `total`。

## 2. search_knowledge

**用途：** 在全部知识库、指定知识库或指定文档中，查找语义相关的标题、正文或图表内容。

### 输入

```json
{
  "query": "高温条件下的性能变化",
  "dataset_ids": ["kb_product"],
  "content_type": "all",
  "top_k": 5,
  "dense_weight": 0.5,
  "sparse_weight": 0.5,
  "rerank": true
}
```

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 问题或关键词，不能为空 |
| `dataset_ids` | string[] | 否 | 限定一个或多个知识库 |
| `document_ids` | string[] | 否 | 限定一篇或多篇文档 |
| `content_type` | string | 否 | `all`、`title`、`text`、`visual`，默认 `all` |
| `top_k` | int | 否 | 最多返回的结果条数，默认 `5` |
| `dense_weight` | float | 否 | 稠密向量检索权重，默认 `0.5` |
| `sparse_weight` | float | 否 | 稀疏检索权重，默认 `0.5` |
| `rerank` | bool | 否 | 是否对检索候选结果进行重排，默认 `True` |

**检索范围：**

- 不传 `dataset_ids` 和 `document_ids`：检索全部允许访问的知识库。
- 只传 `dataset_ids`：检索指定知识库。
- 只传 `document_ids`：检索指定文档。
- 两种 ID 参数不能同时传；传入时数组不能为空，避免范围含义不清。

**内容类型：**

| 值 | 含义 |
| --- | --- |
| `all` | 文档中的全部内容类型 |
| `title` | 文档标题，以及正文中的各级标题 |
| `text` | 正文内容 |
| `visual` | 图片和表格相关内容，两者合并检索 |

仅检索 `title` 时，匹配标题本身，不将标题下的全部正文当成标题命中。命中正文标题时，返回对应页码和标题路径，方便定位章节。

检索结果默认最多返回 5 条，由 `top_k` 控制。`dense_weight` 和 `sparse_weight` 分别控制稠密向量检索与稀疏检索的权重；`rerank` 控制是否重排，开启时返回重排后的前 `top_k` 条结果。片段长度及具体检索、重排算法由后端配置。

其他调用示例：

```json
{
  "query": "设备过热风险"
}
```

```json
{
  "query": "环境要求",
  "document_ids": ["doc_a", "doc_b"],
  "content_type": "title"
}
```

### 返回

```json
{
  "items": [
    {
      "dataset_id": "kb_product",
      "document_id": "doc_a",
      "document_name": "设备使用说明",
      "content_type": "text",
      "content": "设备允许的工作温度为……",
      "page": 12,
      "heading": "使用说明 > 环境要求",
      "document_tokens": 32000,
      "additional_kwargs": {}
    }
  ]
}
```

- `content`：实际命中的标题或内容片段。图表可返回 OCR、图注、表格文本或内容描述。
- `content_type`：实际命中类型，使用 `title`、`text`、`image` 或 `table`；输入中的 `visual` 在返回时区分图片和表格。
- `page`、`heading`：原文页码和标题路径；未知时返回 `null`，不编造位置。
- `document_tokens`：整篇转码后 Markdown 的估计 token 数，用于判断阅读规模；未知时返回 `null`。
- `additional_kwargs`：后端扩展信息，原样透传，不限定内部字段，不做结构解析。可携带已有的图片 URL、文档版本、引用或内容表示方式等信息。

无匹配内容时返回 `{"items": []}`。不返回相关资料总数，也不将前几条语义结果视为全部相关内容。

文档标题命中按文档去重；正文标题按原文位置去重。同一文档可命中多个章节，不同位置的同名标题不能合并。标题结果只提供定位线索，不能代替阅读标题下的正文。

## 3. download_documents

**用途：** 将一篇或多篇文档转码后的 Markdown 保存到当前 Agent 的沙盒目录，供后续直接操作。

### 输入

```json
{
  "document_ids": ["doc_a", "doc_b"]
}
```

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `document_ids` | string[] | 是 | 待下载文档的 ID，至少一篇 |

不要求 Agent 指定保存目录、文件名、版本或下载链接。保存位置和实际传输由工具处理，默认获取当前可用版本。

### 返回

```json
{
  "items": [
    {
      "document_id": "doc_a",
      "document_name": "设备使用说明",
      "path": "/workspace/knowledge/doc_a/document.md",
      "document_tokens": 32000,
      "additional_kwargs": {}
    },
    {
      "document_id": "doc_b",
      "error": "文档尚未转码完成，请稍后重试。"
    }
  ]
}
```

- 成功项返回 `path`，表示文件已保存且能被当前沙盒读取，不是仅获取了下载 URL。示例路径以实际沙盒环境为准。
- `document_tokens` 为整篇 Markdown 的估计 token 数，未知时返回 `null`。
- `additional_kwargs` 为后端扩展信息，原样透传，不限定内部字段，不做结构解析。
- 失败项返回 `document_id` 和 `error`，不返回成功路径。批量请求逐篇返回结果，不能遗漏失败项。
- 图片继续使用 Markdown 内已有的可访问 URL，Agent 需要时再查看图片。
- 页码和标题层级保留在 Markdown 中，便于检索命中后补读上下文。

## 4. Agent 如何选择工具

| 需求 | 调用方式 |
| --- | --- |
| 了解有哪些知识库 | `list_knowledge_bases`，需要时翻页 |
| 普通知识问答 | `search_knowledge`，片段足够时直接回答 |
| 找某份文件或某个章节 | 使用 `content_type: title` 检索 |
| 从几篇资料中快速找相关内容 | 传 `document_ids` 做语义检索 |
| 阅读全文、核实上下文或进行统计 | `download_documents`，再使用沙盒工具 |

指定文档的语义检索与沙盒操作同时保留：语义检索解决“意思相关但关键词不同”的查找；沙盒解决精确匹配、连续阅读、统计和加工。无需固定先列库、再检索、再下载的顺序。

## 5. 实现约定

- 三个工具都只访问当前用户及本轮允许的资料，不能通过下载绕过范围限制。ID 无效或无权限时明确报错，不扩大范围。
- 文档 ID 需要能唯一定位文档，避免 Agent 额外组合嵌套参数。
- 图表语义检索需要后端已有 OCR、图注、表格文本、描述或多模态索引；仅有图片 URL 不代表已经可以检索图片内容。尚不支持时明确报错。
- 页码、标题和内容保持对应。使用生成的图片描述时应说明其性质，不冒充原文。
- 若后端能够识别搜索后文档发生更新，应在下载结果中提示；本方案不向 Agent 增加版本选择参数，也不承诺历史版本下载。
- 转码不完整、图片链接过期等影响阅读的问题，由后端提供清楚提示。
- 当前 DeerFlow 部分逻辑使用工具名 `knowledge_search`；接入本文的 `search_knowledge` 时，需要映射名称或同步调整相关识别逻辑。

不另外增加文档详情、远程阅读、标题过滤、输出预算或复杂结果分类接口。后续确有业务需要再补充。
