# Changelog

## v1.1.0 — 2026-09-10

- Fix active-provider and credential discovery using TOML and explicit credential binding.
- Support CODEX_HOME, provider-specific environment keys, and paired environment overrides.
- Preserve exact requested dimensions and fix all advertised aspect-ratio aliases.
- Add multi-reference editing, prompt files, optional quality, offline dry runs, and read-only diagnostics.
- Add structured redacted errors with request IDs; keep provider, headers, and reference-edit mode stable.
- Default to zero retries; add bounded opt-in HTTP retries and explicit model-only fallback on structured errors.
- Block authenticated redirects and unsafe request headers; never fall back to an unrelated default API host.
- Save compact image results with actual dimensions; stop dumping Base64, raw responses, or rewritten prompts.
- Reject empty/invalid results and keep existing output files intact.
- Add offline regression coverage without requiring live credentials or network access.
- Require Python 3.11+ (standard-library TOML parsing; no external runtime dependencies).

These changes fix local routing, observability, and output handling. They do not
claim to repair a gateway's model routing, upstream account availability, or
service-side 502/503 failures.

## v1.0.0 — 2026-09-05

- Initial public release with gateway-backed generation/editing, local Codex auth
  discovery, image response saving, and a compatible browser User-Agent.
