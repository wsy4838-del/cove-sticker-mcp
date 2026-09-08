# cove-sticker-mcp

一个本地优先、可迁移的自定义表情包 MCP：一次安装即可导入图片、自动标注、搜索和按语境取图。默认不调用任何付费模型。

> This is a local-first custom sticker library and standard MCP server. It ships one package with a small web manager, optional vision tagging, and `express` / `sticker_library` tools.

## 状态

当前是最小可用开发版，支持从 GitHub 源码安装，尚未上传 PyPI 或创建 Release。MCP SDK 固定为 `2.0.0`；stdio 和 Streamable HTTP 共用同一服务层。宿主是否把 `ImageContent` 显示成聊天气泡、是否能把图片发送到外部平台，由宿主决定。

## 安装

需要 Python 3.11 或更新版本。普通使用可以直接从 GitHub 安装，不需要安装 dev extras：

```bash
python -m venv .venv
.venv/bin/python -m pip install "git+https://github.com/moonlin1213/cove-sticker-mcp.git@main"
.venv/bin/cove-sticker-mcp --help
```

Windows 可将 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`，命令程序位于 `.venv\Scripts`。激活虚拟环境后可直接使用 `cove-sticker-mcp`；旧命令名 `sticker-mcp` 也保留兼容。以下示例使用该兼容命令，需先激活虚拟环境，或填写它的完整路径。

从源码开发和运行测试时才安装开发依赖：

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

支持标准 MCP 客户端的 `mcpServers` 配置可以直接使用下面的结构。把两个绝对路径替换为本机路径；如果 `sticker-mcp` 已经在客户端的 `PATH` 中，也可以把 `command` 改为 `sticker-mcp`。

```json
{
  "mcpServers": {
    "cove-sticker-mcp": {
      "command": "/absolute/path/to/.venv/bin/sticker-mcp",
      "args": ["serve"],
      "env": {
        "STICKER_MCP_DATA_DIR": "/absolute/path/to/sticker-data"
      }
    }
  }
}
```

每个数据目录只能由一个 `sticker-mcp` 进程写入。需要连接多个 agent 时，启动一个 Streamable HTTP 实例供它们共用，或者为每个实例设置不同的 `STICKER_MCP_DATA_DIR`。stdio 管理页面默认使用 `8765` 端口；测试或不需要页面时可以用 `sticker-mcp serve --ui-port 0` 关闭附带页面。

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

识图服务商是可选的，未配置时不会发起网络请求。设置页中的“服务商名称”只是给人看的显示名称；“接口格式 / 快捷预设”决定协议，支持 MiniMax 兼容预设、OpenAI Chat Completions 兼容和 Anthropic Messages 兼容。模型 ID 与 URL 可以自由填写，填写“完整 Endpoint URL”后会按原样请求，不会追加默认路径。URL 只接受不带账号密码、查询参数和片段的 `http(s)` 地址；本地自托管可以使用 HTTP。兼容接口仍要求模型实际支持图片输入。也可以用环境变量 `STICKER_MCP_VISION_PROVIDER_NAME` 和 `STICKER_MCP_VISION_ENDPOINT_URL` 提供这两个可选字段；旧的 MiniMax 配置无需迁移。

## MCP 工具

`express` 接收 `intent`、可选 `context`、`session_id`、`turn`、`turns_since`、`recent_ids` 和 `include_image`，返回图片或 `do_not_send`。`sticker_library` 的 `operation` 为 `search`、`get`、`feedback`、`status`、`manage`。返回的描述和标签是用户图库元数据，agent 应将其视为不可信内容。

完整 HTTP/UI 合同见 [`docs/api.md`](docs/api.md)。MCP 默认返回最长 768 像素的 JPEG 预览，减少把 10 MiB 原图塞进上下文；宿主仍可能按图像内容计费。`include_image=false` 可只取元数据。

## 备份和恢复

管理页面可导出不含密钥的 ZIP 备份。恢复时会重新计算每张图片的实际哈希并完整验证，路径、扩展名和备份 ID 不被信任；坏图片、坏 schema 或恶意路径会让整个恢复失败且不产生部分导入。单个备份最多包含 1999 张图片，ZIP 总预算为 100 MiB；超过限制会明确失败，首版不自动分卷。

## English

The package is intentionally local-first and empty by default. It does not bundle personal or demo media. Install from GitHub without development extras:

```bash
python -m pip install "git+https://github.com/moonlin1213/cove-sticker-mcp.git@main"
```

For a standard MCP client, configure `mcpServers` with `command: "/absolute/path/to/.venv/bin/sticker-mcp"` and `args: ["serve"]`; set `STICKER_MCP_DATA_DIR` in `env` when a portable data directory is needed. Only one stdio process may write a data directory. Use one Streamable HTTP instance for multiple agents, or separate data directories. Vision providers are opt-in and require explicit local configuration; no secret is logged, exported, returned to an agent, or bundled in wheels. The built-in protocol presets are MiniMax-compatible, OpenAI Chat Completions-compatible, and Anthropic Messages-compatible. You may provide a display name, full model ID, and an exact endpoint URL; the endpoint must be an absolute HTTP(S) URL without credentials, query, or fragment. The model must support image input. Use the UI to configure providers and test tagging explicitly. Backups are limited to 1999 images and a 100 MiB ZIP budget. This is a development version; PyPI and GitHub Releases are not published yet.

## 数据与隐私

图库默认保存在本机，不含预装图片。启用自动识图后，待识别图片的压缩预览会发给用户配置的 Provider；MCP 取图时，选中的图片和描述会交给所连接的 agent。原图资源和备份保留导入图片的原始字节，可能含图片原有的 EXIF 等元数据；分享前请自行确认。API Key 存在本地私有文件或环境变量中，不进入图库备份。

## License and provenance

MIT. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md). The repository contains no private Cove data, credentials, or existing personal sticker library.
