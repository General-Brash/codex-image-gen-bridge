# codex-image-gen-bridge

**v1.1.0** · 用本机 Codex 配置调用 OpenAI-compatible Images API 的生图 Skill。
支持文生图、多参考图编辑、离线预检和只读诊断，避免把第三方网关密钥误用到官方接口。

> 这是自定义 Skill，不是 OpenAI 官方 SDK，也不会为会话注册原生 `image_gen` 工具。
> 缺少原生工具不代表桥接不能用；桥接收到 502/503 也不代表修改 Skill 就能修好上游服务。

## v1.1.0 修复重点

- 正式解析 TOML，按当前 `model_provider` 选网关，不再抓取文件里的第一个 `base_url`。
- 将网关地址和凭据绑定；支持 `CODEX_HOME`、指定 provider、provider 专属 `env_key`。
- 所有路径保留兼容 User-Agent，禁止携带凭据自动重定向；不自动切换官方接口、SDK 或浏览器。
- 失败输出精简、脱敏的 JSON：HTTP 状态、错误类别、服务端错误码、请求 ID 和明确的重试边界。
- 修正比例映射；`1152x2048` 等显式像素尺寸原样发送，返回实际尺寸和不匹配警告。
- 多个 `--image`、UTF-8 `--prompt-file`、可选质量参数、只读 `--diagnose` 和离线 `--dry-run`。
- 默认不重试；仅显式允许小额度 HTTP 重试和有条件的模型回退，避免重复计费与无依据降级。
- 保存后不再输出巨量 Base64／完整原始响应；空结果、伪图片和无效响应以失败退出。
- 新增不依赖真实凭据、不进行网络请求的回归测试。

## 环境与安装

- **Python 3.11+**，仅使用标准库，无需 `pip install`。
- 一个实际支持 Images API 的网关，以及与该网关配套的 API key。
- 通常读取 `~/.codex/config.toml` 和 `~/.codex/auth.json`；不会读取 OAuth token 充当 API key。

从仓库安装：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone https://github.com/General-Brash/codex-image-gen-bridge.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/codex-image-gen-bridge"
```

若目标目录已存在，先保留本地修改，再更新；不要直接覆盖未提交内容。
也可从 [Releases](https://github.com/General-Brash/codex-image-gen-bridge/releases)
下载 ZIP，验证随附的 SHA-256，将解压后的目录命名为 `codex-image-gen-bridge`
后放入 Skill 目录。发布 ZIP 不包含本机凭据或生成图片。

在 Codex 中明确调用：

```text
使用 $codex-image-gen-bridge，参考我提供的两张图片，通过当前配置的网关生成
一张 1152×2048 的 9:16 角色全身设定图。保留所有参考内容和指定模型。
```

Skill 更新未出现时，可重新打开 Codex。不要通过修改系统 `imagegen` Skill 来假装增加原生工具。

## 配置：地址和密钥必须配对

典型 `config.toml`：

```toml
model_provider = "custom"
[model_providers.custom]
base_url = "https://gateway.example/v1"
wire_api = "responses"
```

对应的 `auth.json`：

```json
{
  "OPENAI_API_KEY": "<your-gateway-api-key>"
}
```

`custom` 可以是任意 provider 名称，不必叫 `sub2api`。
`wire_api = "responses"` 是 Codex 文本模型的配置，不能证明图像端点是否可用；
桥接仍使用 `/images/generations` 和 `/images/edits`。

多 provider 可为每个 provider 配置自己的凭据环境变量：

```toml
[model_providers.other]
base_url = "https://another-gateway.example/v1"
env_key = "OTHER_GATEWAY_API_KEY"
```

先在本机安全设置该环境变量，再传 `--provider other`。选择非当前 provider 时，
桥接拒绝直接复用当前 `auth.json` 的密钥。没有指定 provider 时，只有唯一候选配置
才能使用旧版兼容发现逻辑；有歧义就报错。

**独立脚本环境**也可同时设置 `CODEX_IMAGE_BRIDGE_BASE_URL` 和
`CODEX_IMAGE_BRIDGE_API_KEY`，作为完整的地址／凭据覆盖，不读取 Codex 配置文件。
只设置其中一个会报错，且不能与 `--provider` 混用。

普通 SDK 的 `OPENAI_BASE_URL` / `OPENAI_API_KEY` 不会暗中覆盖本脚本配置；
provider 可以通过 `env_key` 显式指定使用 `OPENAI_API_KEY`。
详见 [配置发现规则](references/config-discovery.md)。

## 使用

以下命令从仓库目录运行；在其他目录使用脚本绝对路径。

### 文生图：精确 16:9 请求

```bash
python3 scripts/bridge.py \
  --model gpt-image-2 --size 2048x1152 \
  --output-dir "$HOME/Pictures/codex-images" \
  "A spring campus wallpaper, soft sunlight and cherry blossoms, no text."
```

### 多参考图编辑：2K、9:16

先将完整提示词写入本机 UTF-8 文件，再运行：

```bash
python3 scripts/bridge.py \
  --mode edit \
  --image /absolute/reference-front.png \
  --image /absolute/reference-details.png \
  --prompt-file /absolute/prompt.txt \
  --model gpt-image-2 --size 1152x2048 --quality high \
  --output-dir "$HOME/Pictures/codex-images"
```

单参考图继续使用兼容的 `image` multipart 字段；多参考图使用 `image[]`。
上传文件名会替换为安全的 ASCII 名称，不将本地路径写入 HTTP 头。
最多 16 张参考图，每张不超过 50 MiB，总计不超过 100 MiB；也支持图片 data URL。

### 离线预检：不请求、不上传、不计费

```bash
python3 scripts/bridge.py --dry-run --size 9:16 "A simple test prompt."
```

预检显示网关、凭据**来源**、请求尺寸等，不显示密钥和提示词正文，也不创建输出目录。

### 只读诊断：仅 GET /models

```bash
python3 scripts/bridge.py --diagnose --model gpt-image-2
```

此操作不会生图、不会上传参考图。模型列表里有某个名字，不保证它的上游可用；
列表里没有，也不能断定兼容网关不支持。正常生图不会被此列表检查阻断。

### 显式且受限的回退／重试

只有已经明确允许备用模型时，才使用：

```bash
python3 scripts/bridge.py --model requested-image-model \
  --fallback-model gpt-image-2 --size 1:1 "A blue circle on white."
```

仅结构化的模型不可用错误触发一次回退；不会将 401、403、普通 404、502/503、
超时或尺寸错误误判成“没有这个模型”。精确模型 ID 按用户和网关约定传递，不自动更名。

`--max-retries 1` 或 `2` 可显式允许少量 HTTP 429/500/502/503/504 重试，默认 `0`。
重试保留地址、模型、参数和请求头，遵守最多 30 秒的 `Retry-After`。
**POST 重试可能重复计费。网络超时不自动重试**，因为服务端可能已接受请求。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `prompt` / `--prompt-file` | 二选一 | 提示词或 UTF-8 提示词文件；诊断模式不需要 |
| `--mode` | `generate` | `generate` 文生图或 `edit` 参考图编辑 |
| `--image` | 无 | 可重复的本地图片路径或 data URL，仅用于编辑 |
| `--model` | `gpt-image-2` | 原样请求指定模型，不自动迁移别名 |
| `--fallback-model` | 无 | 明确授权的单次、条件式模型回退 |
| `--size` | `1:1` | 比例别名、`auto` 或明确 `WIDTHxHEIGHT` |
| `--quality` | 不发送 | 可选 `auto` / `low` / `medium` / `high`，由网关验证支持情况 |
| `--output-dir` | `storage/generated-images` | 保存生成图片，不覆盖已有文件 |
| `--codex-home` | 自动发现 | 优先于 `CODEX_HOME` 和用户主目录 |
| `--provider` | 当前 provider | 指定配置中的 provider，需匹配其凭据 |
| `--timeout` | `120` | socket 超时秒数，最大 `900`，不是整个任务的总时限 |
| `--max-retries` | `0` | HTTP 重试次数，最多 `2`，需明确承担重复调用风险 |
| `--dry-run` | 关闭 | 纯离线配置／输入检查 |
| `--diagnose` | 关闭 | 一次只读模型列表检查，与 dry-run 互斥 |
| `--version` | — | 打印版本 |

比例映射：

| 输入 | 发出的像素尺寸 |
| --- | --- |
| `1:1` | `1024x1024` |
| `16:9` | `2048x1152` |
| `9:16` | `1152x2048` |
| `4:3` | `1536x1152` |
| `3:4` | `1152x1536` |
| `3:2` | `1536x1024` |
| `2:3` | `1024x1536` |

显式像素尺寸不改写；语法限制为正整数、单边不超过 16384。
具体模型／网关可能有更严格限制，会通过 API 错误明确返回。
**请求 2K 不等于实际生成了原生 2K。**

## 输出与错误

成功只打印精简 JSON；示例省略部分路由字段：

```json
{
  "status": "ok",
  "version": "1.1.0",
  "model": "gpt-image-2",
  "requested_size": "1152x2048",
  "file": "/absolute/output/codex-image-example.png",
  "content_type": "image/png",
  "actual_size": "1152x2048",
  "size_matches_request": true
}
```

- 图片二进制和 Base64 响应保存到本地，根据 PNG/JPEG/WebP/GIF 文件头识别格式和尺寸。
- 文件头检查不等于完整解码或视觉质检；必要时仍应打开图片检查。
- 尺寸不符会明确警告；脚本不自动裁切／缩放。保留原图，另存后处理版本，不冒充原生分辨率。
- URL 响应只返回远程地址，不自动下载、不携带 API 凭据访问图片服务器；实际尺寸为 `null`。
- 原始响应、Base64 和改写后的提示词不进入输出。
- 失败向 stderr 输出 JSON 并以退出码 `1` 结束，不再把空结果当成功。

示例错误：

```json
{
  "status": "error",
  "version": "1.1.0",
  "error": {
    "kind": "upstream_unavailable",
    "message": "No available upstream account",
    "http_status": 503,
    "code": "upstream_unavailable",
    "request_id": "example-request-id"
  }
}
```

详见 [错误分类与排查](references/troubleshooting.md)。增大超时不能修复瞬间返回的 503；
若是网关／上游故障，需要利用错误码和请求 ID 排查服务端，而不是盲目换 SDK 或重试。

## 安全与兼容性

- 不提交 `auth.json`、真实 `config.toml`、API key、OAuth token 或私钥；不要在命令行参数中传密钥。
- 默认发送浏览器兼容 User-Agent，可用 `CODEX_IMAGE_BRIDGE_USER_AGENT` 覆盖；拒绝控制字符注入。
- 禁止携带 Authorization 自动重定向到其他 URL；不会自动使用官方 OpenAI 默认地址。
- 使用 HTTPS；仅在明确可信的本地／内网场景使用 HTTP。
- 图片输入的隐私、版权和上传授权由任务所有者确定；不要将诊断误用为图片上传。
- 相比 v1.0.0，最低 Python 版本提高到 3.11，以便无第三方依赖地正确解析 TOML。

## 开发与验证

```bash
python3 -m py_compile scripts/bridge.py
python3 -m unittest discover -s tests -v
python3 scripts/bridge.py --version
```

测试使用临时目录、假凭据和模拟传输，禁止真实联网，不产生生图费用。
测试使用标准库 unittest，可在 Python 3.11+ 上手动运行；本版本不附带 GitHub Actions 工作流。
离线测试不能证明某个网关当下能生成图片；真实生图检查应另行明确范围、次数和费用。

发布包从 Git Tag 构建，附 SHA-256 校验文件；不要将本地输出或凭据打包。
