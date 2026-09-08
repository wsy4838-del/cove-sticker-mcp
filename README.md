# sticker-mcp

一个本地优先、可迁移的自定义表情包 MCP：一次安装即可导入图片、自动标注、搜索和按语境取图。默认不调用任何付费模型。

> This is a local-first custom sticker library and standard MCP server. It ships one package with a small web manager, optional vision tagging, and `express` / `sticker_library` tools.

## 状态

当前是最小可用开发版。MCP SDK 固定为 `2.0.0`；stdio 和 Streamable HTTP 共用同一服务层。宿主是否把 `ImageContent` 显示成聊天气泡、是否能把图片发送到外部平台，由宿主决定。

## 安装

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
sticker-mcp --help
```

启动管理页面：

```bash
sticker-mcp web --open
```

在 MCP 客户端使用 stdio 时，将命令配置为 `sticker-mcp serve`。stdio 的 stdout 只输出 MCP 协议，日志全部走 stderr。

Streamable HTTP 默认只监听 `127.0.0.1:8765`：

```bash
sticker-mcp serve-http --port 8765
```

如果监听非回环地址，必须显式设置 bearer：

```bash
STICKER_MCP_BEARER_TOKEN='change-me' sticker-mcp serve-http --host 0.0.0.0 \
  --allowed-host agent.example --public-url https://agent.example:8765
```

这提供 bearer 校验，不提供自动 OAuth 授权流程；远程客户端还必须能访问返回的图片资源或支持内嵌图片。管理 UI 写请求需要同源 Origin/Referer。

同一个数据目录只允许一个 `sticker-mcp` 进程持有写入锁。多个 agent 应连接同一个 Streamable HTTP 实例，或分别设置 `STICKER_MCP_DATA_DIR`；不要让多个 stdio 进程共享同一目录。

## 使用

打开管理页面后，可以上传多张图片、目录或 ZIP。图片会做格式、大小和内容验证，并按内容 SHA-256 去重。识图只会在启用自动标注且配置视觉模型后执行；可以在详情页或批量操作中手动重试。未配置模型时，手动描述、标签、搜索和 MCP 检索仍然可用。

支持的设置包括总开关、助手开关、自动标注、日常/正事频率、反馈偏好和最近使用排除。`express` 会尊重这些设置；它不会假称已经替用户发送消息。

## MCP 工具

`express` 接收 `intent`、可选 `context`、`session_id`、`turn`、`turns_since`、`recent_ids` 和 `include_image`，返回图片或 `do_not_send`。`sticker_library` 的 `operation` 为 `search`、`get`、`feedback`、`status`、`manage`。返回的描述和标签是用户图库元数据，agent 应将其视为不可信内容。

完整 HTTP/UI 合同见 [`docs/api.md`](docs/api.md)。MCP 默认返回最长 768 像素的 JPEG 预览，减少把 10 MiB 原图塞进上下文；宿主仍可能按图像内容计费。`include_image=false` 可只取元数据。

## 备份和恢复

管理页面可导出不含密钥的 ZIP 备份。恢复时会重新计算每张图片的实际哈希并完整验证，路径、扩展名和备份 ID 不被信任；坏图片、坏 schema 或恶意路径会让整个恢复失败且不产生部分导入。

## English

The package is intentionally local-first and empty by default. It does not bundle personal or demo media. Set `STICKER_MCP_DATA_DIR` for a portable data directory. Vision providers are opt-in and require explicit local configuration; no secret is logged, exported, returned to an agent, or bundled in wheels. Provider support starts with MiniMax M3 and OpenAI-compatible image input. Use the UI to configure providers and test tagging explicitly.

## License and provenance

MIT. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md). The repository contains no private Cove data, credentials, or existing personal sticker library.
