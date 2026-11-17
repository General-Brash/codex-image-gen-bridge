# Configuration discovery

## Codex home

Order: `--codex-home` > `CODEX_HOME` > `USERPROFILE/.codex` or `HOME/.codex`.
The script reads configuration in place; it never rewrites Codex settings.
Python 3.11+ supplies the standard-library TOML parser (`tomllib`).

The default `--config-source codex` always reads Codex configuration. Neither
bridge-specific nor generic SDK environment variables can silently change the
host or bypass a missing/invalid Codex config.

If the calling Codex session uses `--profile NAME`, pass the same option here.
The bridge overlays `<CODEX_HOME>/<NAME>.config.toml` on `config.toml`, merging
TOML tables recursively. It cannot infer a profile or one-off CLI overrides from
another process. Provider and credential settings stay in user/profile config;
do not scan arbitrary project files for the first available base URL.

Profile changes must preserve credential binding. Switching providers cannot
reuse the base provider's `auth.json` key. Changing an existing provider's base
URL requires an explicit matching `env_key` or `experimental_bearer_token` in
that profile. A profile bearer token that would lose to an inherited `env_key`
is rejected, not silently substituted. Use a separate provider for a new site,
or explicitly configure its matching `env_key` in the profile.

## Provider and credential are a pair

In the default Codex configuration mode:

1. Parse `config.toml` as TOML, including comments, quoted tables, and strings.
2. Apply an explicitly selected profile, then select `--provider NAME`, otherwise
   the effective `model_provider`.
3. If neither is present, accept exactly one provider with `base_url`, or a sole
   legacy top-level `base_url`. Reject ambiguous configurations instead of
   picking the first `base_url` anywhere in the file.
4. Read that provider's `env_key` environment variable when configured. An unset
   variable is an error; never substitute another provider's credential.
5. Otherwise, use the selected provider's `experimental_bearer_token` when
   configured. It is an explicit provider credential, not a ChatGPT OAuth token
   extracted from `auth.json`. Never print its value or mix it with another host.
6. Otherwise, use `OPENAI_API_KEY` (`openai_api_key` / `api_key` also supported)
   from `auth.json`, bound to the active or unambiguous legacy provider. An
   explicitly selected non-active provider needs its own `env_key` or
   `experimental_bearer_token`. OAuth access/refresh tokens are never substituted.

Example (the table name need not be `sub2api`):

```toml
model_provider = "custom"
[model_providers.custom]
base_url = "https://gateway.example/v1"
wire_api = "responses"
# Optional; when set, it replaces auth.json credential discovery:
# env_key = "MY_GATEWAY_API_KEY"
```

Existing Codex configurations with a provider-bound `experimental_bearer_token`
work without copying the token into `auth.json`. Prefer `env_key` for new setups.

`wire_api` configures Codex's text-model connection. It does not establish which
image models/routes the gateway supports and does not change this bridge from
`/images/*` into `/responses`.

## Explicit environment-only operation

Only when the user explicitly chooses a separate environment-only route, pass
`--config-source environment` and set **both** `CODEX_IMAGE_BRIDGE_BASE_URL` and
`CODEX_IMAGE_BRIDGE_API_KEY` locally. This mode does not read Codex config/auth
files and cannot be combined with `--provider` or `--profile`.
With the default source, these variables are ignored, even if only one is set.
Never put a real key in prompts, CLI arguments, shared logs, or source control.

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

`--dry-run` reports config source, profile, provider, base URL, credential
**source**, endpoint, requested model, resolved size, and reference count.
It does not report the credential or prompt content,
create output files, upload images, or perform network calls.
