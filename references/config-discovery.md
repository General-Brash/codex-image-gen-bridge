# Configuration discovery

## Codex home

Order: `--codex-home` > `CODEX_HOME` > `USERPROFILE/.codex` or `HOME/.codex`.
The script reads configuration in place; it never rewrites Codex settings.
Python 3.11+ supplies the standard-library TOML parser (`tomllib`).

## Provider and credential are a pair

Without explicit environment overrides:

1. Parse `config.toml` as TOML, including comments, quoted tables, and strings.
2. Select `--provider NAME`, otherwise the top-level `model_provider`.
3. If neither is present, accept exactly one provider with `base_url`, or a sole
   legacy top-level `base_url`. Reject ambiguous configurations instead of
   picking the first `base_url` anywhere in the file.
4. Read that provider's `env_key` environment variable when configured. An unset
   variable is an error; never substitute another provider's credential.
5. Otherwise, use `OPENAI_API_KEY` (`openai_api_key` / `api_key` also supported)
   from `auth.json`, bound to the active or unambiguous legacy provider. An
   explicitly selected non-active provider needs its own `env_key`.

Example (the table name need not be `sub2api`):

```toml
model_provider = "custom"
[model_providers.custom]
base_url = "https://gateway.example/v1"
wire_api = "responses"
# Optional; when set, it replaces auth.json credential discovery:
# env_key = "MY_GATEWAY_API_KEY"
```

`wire_api` configures Codex's text-model connection. It does not establish which
image models/routes the gateway supports and does not change this bridge from
`/images/*` into `/responses`.

## Explicit environment-only operation

Set **both** `CODEX_IMAGE_BRIDGE_BASE_URL` and `CODEX_IMAGE_BRIDGE_API_KEY` locally
to use a separate credential/host pair without reading Codex config/auth files.
Do not combine that pair with `--provider`. Never put a real key in prompts,
CLI arguments, shared logs, or source control.

Generic SDK variables `OPENAI_BASE_URL` / `OPENAI_API_KEY` do not override the
bridge implicitly. A configured provider may explicitly name `OPENAI_API_KEY`
as its `env_key`. This prevents unrelated SDK environment settings from changing
only one half of the gateway/key pair.

## URL and transport rules

- Retain a base URL ending in `/v1`; otherwise append `/v1` once. Preserve an
  existing path prefix, for example `/proxy/v1`.
- Reject embedded credentials, query/fragment parameters, and full operation
  URLs mistakenly supplied as base URLs. Never guess `api.openai.com`.
- Generate: `POST {normalized_base_url}/images/generations`.
- Edit: `POST {normalized_base_url}/images/edits`.
- Diagnose: `GET {normalized_base_url}/models` only.
- Every request uses the same bearer credential and browser-compatible
  `User-Agent`. Override only the latter using `CODEX_IMAGE_BRIDGE_USER_AGENT`.
- Authenticated redirects are blocked to avoid credential forwarding. Check
  the configured URL rather than following a redirect to a different service.
- HTTPS is recommended; use HTTP only for a deliberately trusted local/network
  service. Normal system proxy environment variables are honored by urllib.

`--dry-run` reports provider, base URL, credential **source**, endpoint, resolved
size, and reference count. It does not report the credential or prompt content,
create output files, upload images, or perform network calls.
