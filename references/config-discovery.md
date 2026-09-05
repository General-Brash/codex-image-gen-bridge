# Config Discovery

Read these local files from the user's Codex home:

- `~/.codex/auth.json`
- `~/.codex/config.toml`

Expected minimal fields:

- `auth.json`: `OPENAI_API_KEY`
- `config.toml`: `model_providers.sub2api.base_url`
- Optional `config.toml`: `model_provider`, `model`, `model_reasoning_effort`
- Optional environment variable: `CODEX_IMAGE_BRIDGE_USER_AGENT`

Endpoint rules:

- `generate` mode -> `POST {base_url}/v1/images/generations`
- `edit` mode -> `POST {base_url}/v1/images/edits`

Output rules:

- Never print the API key.
- Return the saved file path when the API returns image bytes or base64.
- Return the image URL when the API returns a URL.

Request compatibility:

- The bridge uses a browser-compatible `User-Agent` by default for gateways
  that reject Python's default `urllib` signature.
- Set `CODEX_IMAGE_BRIDGE_USER_AGENT` to override it without changing the
  script.
