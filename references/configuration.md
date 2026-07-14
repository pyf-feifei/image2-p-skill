# Image2 P Configuration

Read this reference when configuring credentials, separate providers, custom
endpoints, or chatgpt2api behavior.

## Variable file

The default file is `~/.config/image2-p/config.env`. Override it with
`--env-file PATH` or `IMAGE2_P_ENV_FILE`. The script never loads a working
directory `.env` file.

Configure separate generation and editing services:

```dotenv
IMAGE2_P_GENERATE_BASE_URL=https://generate.example.com
IMAGE2_P_GENERATE_API_KEY=replace-me
IMAGE2_P_GENERATE_MODEL=gpt-image-2

IMAGE2_P_EDIT_BASE_URL=https://edit.example.com
IMAGE2_P_EDIT_API_KEY=replace-me
IMAGE2_P_EDIT_MODEL=gpt-image-2
```

Use `IMAGE2_P_BASE_URL`, `IMAGE2_P_API_KEY`, and `IMAGE2_P_MODEL` for one shared
service. Operation-specific values take precedence.

Optional values:

```dotenv
IMAGE2_P_GENERATE_ENDPOINT=/v1/images/generations
IMAGE2_P_EDIT_ENDPOINT=/v1/images/edits
IMAGE2_P_GENERATE_SOURCE=auto
IMAGE2_P_EDIT_SOURCE=auto
IMAGE2_P_ALLOW_INSECURE_HTTP=false
```

`SOURCE` accepts `auto`, `process`, `file`, or `codex`. A non-loopback HTTP URL
requires `IMAGE2_P_ALLOW_INSECURE_HTTP=true`; prefer HTTPS because bearer keys,
prompts, generated URLs, and source images otherwise travel without encryption.

Do not put this variable file inside the Skill directory. Skill instructions
may be loaded into an agent's context, while the external file is only parsed by
the script.

## Codex configuration fallback

Use `--source codex` to force `$CODEX_HOME/config.toml` or
`~/.codex/config.toml`. The script first accepts a complete pair from:

```toml
[shell_environment_policy.set]
OPENAI_BASE_URL = "https://proxy.example.com/v1"
OPENAI_API_KEY = "replace-me"
```

It otherwise reads the active provider:

```toml
model_provider = "custom"

[model_providers.custom]
base_url = "https://proxy.example.com/v1"
experimental_bearer_token = "replace-me"
```

An `env_key` provider field is also supported. Provider authentication commands
are not executed.

## Commands

Validate without a request or output files:

```powershell
python scripts/image2_p.py generate --source file --dry-run
python scripts/image2_p.py edit --source codex --image input.png --prompt "Edit it" --dry-run
```

Use a custom full endpoint or relative endpoint:

```powershell
python scripts/image2_p.py generate --endpoint /v1/images/generations --dry-run
```

CLI credentials must be supplied as a complete pair:

```powershell
python scripts/image2_p.py generate --base-url https://proxy.example.com --api-key VALUE
```

## chatgpt2api 1.4.1 compatibility

- Generation: `POST /v1/images/generations` with JSON.
- Editing: `POST /v1/images/edits` with multipart local files.
- Authentication: `Authorization: Bearer`.
- Image count: `n` from 1 through 4.
- Preferred response: `response_format=b64_json`.
- Multiple edit images are supported.
- Remote image URL behavior varies by release; local multipart files are the
  reliable path.
- Mask support was added after 1.4.1. That release silently ignores masks.
- Supported image models include `gpt-image-2`, `codex-gpt-image-2`, and
  account-tier-prefixed Codex variants. Availability depends on the account
  pool behind the service.

Source documentation:

- https://github.com/basketikun/chatgpt2api/blob/v1.4.1/README.md
- https://github.com/basketikun/chatgpt2api/blob/v1.4.1/api/ai.py
- https://github.com/basketikun/chatgpt2api/blob/v1.4.1/api/image_inputs.py
