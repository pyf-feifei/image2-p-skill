# image2-p

面向 Codex、Claude Code 和 Grok 的图片生成/编辑 Skill。通过一个纯 Python 3.11
命令行工具调用 OpenAI 兼容的 Images API，支持 `chatgpt2api`、`sub2api`、
`gpt-image-2`，并兼容原 `image2-proxy` 的生成工作流。

## 功能

- 调用 `POST /v1/images/generations` 生成图片。
- 调用 `POST /v1/images/edits` 编辑本地图片。
- 支持多图编辑、远程图片 URL、多个输出和可选 mask。
- 为生成与编辑分别配置 URL、key、模型和端点。
- 可直接读取 Codex `config.toml` 中当前 provider 的 sub2api 配置。
- 兼容 `image2-proxy` 的无子命令参数和 `generate_image2.py` 入口。
- 自动保存图片与响应 JSON，并防止覆盖输入图片。
- dry-run、响应脱敏、HTTP 明文保护和返回 URL 安全检查。
- 仅使用 Python 3.11 标准库，无第三方运行时依赖。

## 安装

推荐把仓库作为用户级 Skill 安装到 `~/.agents/skills/image2-p`。Codex 和 Grok
可直接发现该目录，再为 Claude Code 创建同一目录的链接。

### Windows PowerShell

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
git clone https://github.com/pyf-feifei/image2-p-skill.git `
  "$HOME\.agents\skills\image2-p"

New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
New-Item -ItemType Junction `
  -Path "$HOME\.claude\skills\image2-p" `
  -Target "$HOME\.agents\skills\image2-p"
```

旧版 Codex 只扫描 `~/.codex/skills` 时，可增加兼容入口：

```powershell
New-Item -ItemType Directory -Force "$HOME\.codex\skills" | Out-Null
New-Item -ItemType Junction `
  -Path "$HOME\.codex\skills\image2-p" `
  -Target "$HOME\.agents\skills\image2-p"
```

### macOS / Linux

```bash
mkdir -p ~/.agents/skills ~/.claude/skills
git clone https://github.com/pyf-feifei/image2-p-skill.git \
  ~/.agents/skills/image2-p
ln -s ~/.agents/skills/image2-p ~/.claude/skills/image2-p
```

安装后重新打开客户端会话。Grok 可用 `grok inspect --json` 检查 Skill 是否被
发现。

## 配置

默认变量文件是：

```text
~/.config/image2-p/config.env
```

生成与编辑可以使用不同服务：

```dotenv
IMAGE2_P_GENERATE_BASE_URL=https://generate.example.com
IMAGE2_P_GENERATE_API_KEY=replace-me
IMAGE2_P_GENERATE_MODEL=gpt-image-2

IMAGE2_P_EDIT_BASE_URL=https://edit.example.com
IMAGE2_P_EDIT_API_KEY=replace-me
IMAGE2_P_EDIT_MODEL=gpt-image-2

IMAGE2_P_ALLOW_INSECURE_HTTP=false
```

若两种操作使用同一服务，可改用：

```dotenv
IMAGE2_P_BASE_URL=https://images.example.com
IMAGE2_P_API_KEY=replace-me
IMAGE2_P_MODEL=gpt-image-2
```

不要把真实 key 或 `config.env` 放入仓库。完整的变量、配置优先级、Codex
provider 回退和自定义端点说明见
[`references/configuration.md`](references/configuration.md)。

## 使用 Codex 配置

`--source codex` 会读取 `$CODEX_HOME/config.toml`，未设置 `CODEX_HOME` 时读取
`~/.codex/config.toml`：

```powershell
python scripts/image2_p.py generate --source codex --dry-run
```

支持 `[shell_environment_policy.set]` 中的 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，
以及当前 `[model_providers]` 项的 `base_url`、`experimental_bearer_token` 或
`env_key`。

## 命令行使用

以下命令均从仓库根目录执行。

### 检查配置

`--dry-run` 不发送网络请求，也不写入输出文件：

```powershell
python scripts/image2_p.py generate --source file --dry-run
python scripts/image2_p.py edit --source file `
  --image input.png `
  --prompt "Keep the composition and use warmer lighting" `
  --dry-run
```

### 生成图片

```powershell
python scripts/image2_p.py generate `
  --prompt "A polished 16:9 game environment concept, no text" `
  --size 1536x864 `
  --quality medium `
  --out output/imagegen/concept.png `
  --response-out output/imagegen/concept-response.json
```

### 编辑图片

```powershell
python scripts/image2_p.py edit `
  --prompt "Preserve the composition and change the sky to sunrise" `
  --image input/scene.png `
  --out output/imagegen/scene-edited.png `
  --response-out output/imagegen/scene-edited-response.json
```

重复 `--image` 可上传多张本地图片。优先使用本地文件；不同版本代理对
`--image-url` 和 `--mask` 的支持程度可能不同。

### image2-proxy 兼容入口

```powershell
python scripts/generate_image2.py `
  --prompt "A clean product render" `
  --size 1024x1024 `
  --quality low
```

也可以直接省略 `generate`：

```powershell
python scripts/image2_p.py --prompt "A clean product render" --dry-run
```

## 在 AI 客户端中调用

- Codex：在请求中写 `Use $image2-p to generate ...`。
- Claude Code：使用 `/image2-p ...` 或直接描述图片任务。
- Grok：使用 `/image2-p ...`，也支持根据 Skill 描述自动调用。

Skill 的代理执行说明位于 [`SKILL.md`](SKILL.md)。

## chatgpt2api 兼容说明

本项目按 `chatgpt2api 1.4.1` 的接口行为验证：

- 生图请求使用 JSON，编辑请求使用 multipart/form-data。
- `n` 支持 `1` 到 `4`。
- 客户端优先请求 `response_format=b64_json`。
- 本地多图编辑可用。
- 1.4.1 不支持 mask，传入 mask 会被忽略。
- 图片接口是否支持某个模型取决于服务端账号池。

## 安全

- 优先使用 HTTPS。非本机 `http://` 服务必须显式设置
  `IMAGE2_P_ALLOW_INSECURE_HTTP=true` 或传入 `--allow-insecure-http`。
- HTTP 会让 bearer key、提示词和编辑源图以明文传输。
- 不要通过命令行参数长期传递 key；命令行可能进入 shell 历史或进程列表。
- 不要打印变量文件、Codex 配置或 Authorization header。
- 返回响应中的 key 和签名 URL 查询参数会在落盘前脱敏。

## 测试

```powershell
python -m unittest discover -s tests -v
```

Skill 结构校验需要 Codex 自带的 `skill-creator` 校验脚本；普通运行与测试不依赖
Codex 或第三方 Python 包。

## 目录结构

```text
image2-p/
|-- SKILL.md
|-- README.md
|-- agents/
|   `-- openai.yaml
|-- references/
|   `-- configuration.md
|-- scripts/
|   |-- image2_p.py
|   `-- generate_image2.py
`-- tests/
    `-- test_image2_p.py
```
