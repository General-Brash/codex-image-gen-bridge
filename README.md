# codex-image-gen-bridge

一个用于 Codex 的本地图片生成桥接 skill，通过用户本机的 Codex 配置调用 OpenAI-compatible 图片接口，支持文生图和参考图编辑。

> 本项目是本地自定义 Codex skill，不是 OpenAI 官方 SDK 或官方 Marketplace skill。它不会自动修改所有 Codex 对话的路由；使用时应显式调用本 skill 或直接运行 `scripts/bridge.py`。

## 功能

- 支持图片生成：`POST /v1/images/generations`
- 支持参考图编辑：`POST /v1/images/edits`
- 默认使用 `gpt-image-2` 请求模型，可通过参数覆盖
- 支持图片 URL、base64 JSON 和直接图片响应
- 自动将 base64 图片保存到本地输出目录
- 默认发送浏览器兼容的 `User-Agent`，适配会拦截 Python 默认 `urllib` 请求特征的兼容网关
- 支持通过环境变量覆盖 `User-Agent`
- 仅使用 Python 标准库，无需安装第三方依赖

## 环境要求

- Python 3.10 或更高版本
- 已安装并能使用 Codex
- 一个兼容 OpenAI Images API 的网关或服务
- 本地存在 Codex 配置文件：
  - `~/.codex/auth.json`
  - `~/.codex/config.toml`

脚本使用了 Python 3.10 的类型标注语法，例如 `str | None`，因此不建议使用 Python 3.9 或更低版本。

## 安装

### 方式一：安装到 Codex skill 目录

```bash
mkdir -p "$HOME/.codex/skills"
git clone https://github.com/General-Brash/codex-image-gen-bridge.git \
  "$HOME/.codex/skills/codex-image-gen-bridge"
```

如果目标目录已经存在，请先确认其中没有需要保留的本地修改，再自行更新仓库；不要用覆盖操作直接丢弃未提交改动。

安装后可以执行语法检查：

```bash
python3 -m py_compile \
  "$HOME/.codex/skills/codex-image-gen-bridge/scripts/bridge.py"
```

### 方式二：直接使用脚本

也可以只下载本仓库，然后直接运行：

```bash
python3 scripts/bridge.py "A watercolor illustration of a small orange cat reading under a tree."
```

## 配置

### `auth.json`

脚本会从 `~/.codex/auth.json` 中读取 `OPENAI_API_KEY` 字段。示例结构如下：

```json
{
  "OPENAI_API_KEY": "<your-api-key>"
}
```

请将真实密钥保存在本机，不要写入仓库、README、命令历史或公开 issue。

### `config.toml`

推荐使用独立的 `model_providers.sub2api` 配置段：

```toml
[model_providers.sub2api]
wire_api = "responses"
base_url = "https://your-compatible-gateway.example/v1"
```

`base_url` 可以带或不带 `/v1`，脚本会自动规范化。最终请求地址为：

```text
{base_url}/images/generations
{base_url}/images/edits
```

脚本优先读取 `[model_providers.sub2api]` 中的 `base_url`；如果没有该配置段，则会兼容读取配置文件中找到的第一个 `base_url`。为了避免多 provider 时产生歧义，建议明确配置 `model_providers.sub2api`。

### User-Agent 覆盖

部分兼容网关会因为 Python 默认的 `urllib` 请求特征触发边缘风控，返回 Cloudflare `403` 或 Error 1010。脚本默认发送浏览器兼容的 User-Agent。

如果你的网关有自己的请求头要求，可以在不修改脚本的情况下覆盖：

```bash
export CODEX_IMAGE_BRIDGE_USER_AGENT="Mozilla/5.0 ..."
```

该变量只影响当前进程及其子进程，不会修改 Codex 全局配置。

## 使用方法

### 文生图

```bash
python3 scripts/bridge.py \
  --model gpt-image-2 \
  --size 16:9 \
  --output-dir "$HOME/Pictures/codex-images" \
  "A cinematic spring campus wallpaper, soft sunlight, cherry blossoms, no text."
```

### 参考图编辑

```bash
python3 scripts/bridge.py \
  --mode edit \
  --image "/absolute/path/to/reference.png" \
  --size 16:9 \
  --output-dir "$HOME/Pictures/codex-images" \
  "Keep the character identity and place her in a bright spring school campus."
```

建议使用绝对路径传入参考图片，避免从不同工作目录运行时出现路径错误。

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `prompt` | 必填 | 图片生成或编辑提示词 |
| `--mode` | `generate` | `generate` 文生图，`edit` 参考图编辑 |
| `--image` | 无 | `edit` 模式下的本地图片路径或 data URL |
| `--size` | `1:1` | 逻辑尺寸比例，支持 `1:1`、`4:3`、`16:9`、`9:16` |
| `--model` | `gpt-image-2` | 请求使用的模型名 |
| `--output-dir` | `storage/generated-images` | base64 或图片响应的保存目录 |

兼容网关可能会将请求尺寸转换为自身支持的输出尺寸，因此建议对最终文件的实际尺寸和比例进行验证。

## 输出格式

脚本会打印 JSON 结果：

- API 返回图片二进制：保存为本地 PNG/JPEG/WebP/GIF 文件；
- API 返回 base64：解码并保存为本地 PNG 文件；
- API 返回 URL：返回 URL，同时保留原始结果；
- API 没有返回可识别图片：返回原始 JSON 结果，方便排查网关兼容性。

示例：

```json
{
  "file": "/Users/you/Pictures/codex-images/codex-image-1234567890.png",
  "content_type": "image/png"
}
```

## 在 Codex 中使用

将本仓库安装到 `~/.codex/skills/codex-image-gen-bridge` 后，在新对话中显式调用：

```text
使用 $codex-image-gen-bridge，通过本地 bridge 生成一张横向春日校园壁纸。
```

也可以明确要求使用脚本：

```text
请使用 ~/.codex/skills/codex-image-gen-bridge/scripts/bridge.py，
不要调用其他图片生成路径。
```

仅在提示词中写“请把请求发到某个网址”，并不能保证 Codex 自动切换到本地 bridge。

## 安全说明

- 不要提交 `auth.json`、API key、OAuth token、私钥或包含凭据的配置文件。
- 不要把真实密钥写入命令行参数、README 或公开日志。
- 本脚本不会打印 API key，但输出结果可能包含网关返回的原始 JSON，请注意其中是否有敏感字段。
- 输入图片的版权、隐私和使用授权由使用者负责。
- 图片模型、网关和上游服务的使用条款由使用者自行确认。
- 遇到图片生成请求失败时不要盲目重复调用，以免产生重复请求或费用。

## 开发与验证

本项目没有第三方 Python 依赖。最小验证命令：

```bash
python3 -m py_compile scripts/bridge.py
```

真实接口验证需要本机配置有效的 `auth.json` 和 `config.toml`，并且可能产生图片生成费用。建议先验证网关的 `/v1/models` 或其他低风险健康检查接口，再执行实际图片请求。

## 目录结构

```text
codex-image-gen-bridge/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
├── references/
│   └── config-discovery.md
└── scripts/
    └── bridge.py
```

## 版本

当前发布版本：**v1.0.0**

本版本包含：

- 初始公开仓库结构；
- 文生图与参考图编辑桥接；
- 本地配置和认证读取；
- base64、URL、二进制图片响应处理；
- 兼容 Cloudflare/网关请求特征检查的默认 User-Agent；
- `CODEX_IMAGE_BRIDGE_USER_AGENT` 环境变量覆盖支持。
