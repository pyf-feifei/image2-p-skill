---
name: image2-p
description: Generate and edit raster images through OpenAI-compatible image proxies, including chatgpt2api and sub2api. Use when Codex, Claude Code, or Grok needs to create an image, modify one or more local images, save image API output, use gpt-image-2, or replace the image2-proxy workflow with separate generation and editing credentials.
---

# Image2 P

Use the bundled `scripts/image2_p.py` for both generation and editing. It uses
only the Python 3.11 standard library and never prints resolved API keys.

## Workflow

1. Turn the request into a concrete prompt. Preserve requested subject,
   composition, aspect ratio, style, lighting, exact text, and exclusions.
2. Choose `generate` for a new image or `edit` when source images are supplied.
3. Run the script from the current `SKILL.md` directory. Use a project-local
   `output/imagegen/` path unless the user names another location.
4. Inspect every saved image with the client's local image-viewing tool. Check
   that it is nonempty, correctly framed, and matches the request.
5. Report the image paths, response JSON path, operation, model, size, quality,
   and final prompt. Do not report credentials.

## Generate

```powershell
python path\to\image2-p\scripts\image2_p.py generate `
  --prompt "A polished 16:9 game environment concept" `
  --size 1536x864 `
  --quality medium `
  --out output\imagegen\concept.png `
  --response-out output\imagegen\concept-response.json
```

The legacy `image2-proxy` form is also valid: omit `generate` and pass the old
flags directly. `scripts/generate_image2.py` is a compatibility entry point.

## Edit

```powershell
python path\to\image2-p\scripts\image2_p.py edit `
  --prompt "Keep the composition; change the sky to a clear sunrise" `
  --image input\scene.png `
  --out output\imagegen\scene-edited.png `
  --response-out output\imagegen\scene-edited-response.json
```

Repeat `--image` for multi-image editing. Prefer local files. `--image-url` is
available when the configured server supports remote inputs. `--mask` is
forward-compatible, but chatgpt2api 1.4.1 ignores masks; do not promise masked
editing against that version.

## Configuration

Use `--source auto` by default. Resolution uses a complete URL/key pair from
one source and never combines a key from one source with another source's URL:

1. `--base-url` plus `--api-key`
2. operation-specific process variables
3. common process variables
4. operation-specific values in the variable file
5. common values in the variable file
6. legacy `OPENAI_BASE_URL` plus `OPENAI_API_KEY`
7. `$CODEX_HOME/config.toml`, then `~/.codex/config.toml`

Use `--source codex` to force the active Codex provider, or `--source file` to
force the variable file. Run `--dry-run` to validate configuration without a
network request or output writes. Read `references/configuration.md` when
setting up credentials, separate generation/editing services, custom endpoints,
or insecure HTTP.

## Safety

- Never place a key in a prompt, command example, output filename, response, or
  Skill file.
- Never print the variable file or `config.toml` while troubleshooting.
- Require explicit `IMAGE2_P_ALLOW_INSECURE_HTTP=true` (or
  `--allow-insecure-http`) for a non-loopback `http://` endpoint. HTTP exposes
  the bearer key, prompts, and editing images in transit.
- Do not automatically load `.env` from the working directory. Only load the
  explicit/default user configuration file.
- Do not overwrite source images. Use a distinct output path.
