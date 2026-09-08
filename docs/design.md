# Sticker MCP 设计

## 目标

`sticker-mcp` 是一个单安装包：本地表情图库、导入与安全校验、可选的导入时识图、检索与偏好管理页面，以及标准 MCP 服务共用同一份 SQLite 数据和媒体目录。它面向能调用 MCP 工具的 agent，但不声称宿主一定能把图片渲染成聊天气泡或直接发送到第三方聊天平台。

首版的两个 MCP 工具是：

* `express`：按语境和会话冷却规则挑一张合适的表情，返回 `ImageContent`、稳定 ID 和不可信的描述元数据；没有合适候选时返回明确的 `do_not_send`。
* `sticker_library`：一个有限的操作入口，支持 `search`、`get`、`feedback`、`status`、`manage`。导入、标注、设置、备份恢复通过管理页面或 CLI 完成，避免把文件系统管理暴露成一组高风险工具。

## 分层

* `library.py`：SQLite 事务、内容哈希、标签检索、软删除、反馈、会话频率与设置。
* `assets.py`：媒体路径只由内容哈希生成；Pillow 解码验证；尺寸、格式、ZIP 预算与路径穿越防护。
* `vision.py`：无密钥时完全禁用网络；MiniMax M3 和 OpenAI-compatible 只接受显式配置；响应 JSON 受限、图片缩小、超时和单 worker 队列。
* `web.py`：同进程静态管理页面和回环 HTTP API；写请求要求同源 `Origin`/`Referer`，远程 HTTP 由 bearer 鉴权保护。
* `mcp_server.py`：官方 Python MCP SDK 2.0.0；stdio 与 Streamable HTTP 复用服务。stdio 的任何日志只走 stderr，HTTP 的图片使用 SDK `ImageContent`，文本明确标注为图库元数据。

## 数据和安全边界

默认数据目录由 `platformdirs` 决定，也可用 `STICKER_MCP_DATA_DIR` 指定。图片放在 `assets/`，SQLite 放在 `library.sqlite3`，用户设置放在 SQLite；密钥只来自本地环境变量或本地私有配置文件，不进数据库导出、备份、返回内容、日志或静态资源。

默认只监听 `127.0.0.1`。`--host` 不是回环地址时必须同时提供 `--bearer-token`（或 `STICKER_MCP_BEARER_TOKEN`），否则拒绝启动。浏览器管理写请求检查 Origin/Referer，并使用内存 session CSRF token；MCP HTTP 使用 bearer。项目不抓取远程 URL、不接受任意文件路径读取，ZIP 先逐项检查大小、数量、压缩比、规范化路径和目标目录再解压。

恢复不会信任备份中的 `id`、扩展名或路径。每张图片先在临时目录按实际字节重算 SHA-256、重新解码并验证字段；全部通过后才在一个 SQLite 事务中合并。任意一张失败都不产生部分导入。

软删除保留媒体和历史引用，但 `express` 和 agent-facing `get` 默认排除 `deleted`、总开关关闭、助手开关关闭或冷却不满足的内容；管理页面可以查看和恢复。

## 成本与宿主限制

自动识图只在导入、显式批量识图或显式重试时发生，使用受限的单 worker。进程重启只恢复“可重试”状态，不自动发起付费请求。导入没有视觉模型配置时仍可手动编辑和检索。MCP 客户端不一定回传真实对话轮次，因此 `express` 接收显式 `session_id`、`turn` 或 `turns_since`；没有这些信息时只做静态频率判断，文档不假设服务能自动知道宿主会话状态。

## 兼容性

MCP SDK 主版本固定为 `2.0.0`，避免随未稳定 API 漂移。stdio 适合本机客户端；Streamable HTTP 适合单用户远程实例，但客户端必须能访问图片资源或接受内嵌图片。项目不实现 OAuth 自动授权，不把 bearer 误称为通用 OAuth 兼容。Cove 可通过自己的现有图片渲染适配器消费同一库；本项目不依赖 Cove。

