---
name: codex-image-gen-bridge
description: Local image-generation bridge that reads the user's Codex auth and config files, discovers the sub2api OpenAI-compatible endpoint and API key, and runs gpt-image-2 image generation or image editing from a standalone script. Use when you need Codex or a third-party script to generate or edit images through the user's local sub2api setup and return a usable image URL or saved file path.
---

# Codex Image Gen Bridge

## Overview

Use this skill when an image job must be executed from the user's local Codex environment without hardcoding secrets. The bridge reads `~/.codex/auth.json` and `~/.codex/config.toml`, extracts the OpenAI API key and the configured sub2api base URL, then calls the image endpoint with `gpt-image-2`.

## Workflow

1. Resolve the Codex home directory from `USERPROFILE` or `HOME`.
2. Read `auth.json` for `OPENAI_API_KEY`.
3. Read `config.toml` for `model_providers.sub2api.base_url` and `wire_api`.
4. Build the final endpoint:
   - `images` mode -> `POST /v1/images/generations`
   - `edit` mode -> `POST /v1/images/edits`
5. Run the bundled script and return the saved file path and public URL.

The bridge sends a browser-compatible `User-Agent` by default because some
compatible gateways reject Python's default `urllib` signature at the edge.
Override it for a specific environment with `CODEX_IMAGE_BRIDGE_USER_AGENT`.

## Usage

- Prefer the script in `scripts/bridge.py` for actual calls.
- Use `generate` for prompt-only image generation.
- Use `edit` when an input image should be modified.
- Keep the output concise: return the image URL, the local file path, and the detected provider summary.

### Examples

```bash
python scripts/bridge.py "A cute orange cat astronaut sticker on a clean pastel background."
python scripts/bridge.py --mode edit --image "C:\path\input.png" "Make it a blue sticker icon."
```

## Safety

- Never print the API key.
- Never assume the config file layout; parse defensively.
- Fail loudly if no usable key or base URL is found.
