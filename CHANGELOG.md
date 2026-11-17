# Changelog

## v1.2.0 - 2026-11-17

- Request gpt-image-2.6 by default; preserve explicit model IDs without alias rewriting or automatic downgrades.
- Make Codex configuration the default source; require explicit environment-only selection to bypass it.
- Support the selected provider's experimental_bearer_token without extracting ChatGPT OAuth tokens.
- Support named Codex profile file overlays, optional xhigh/max quality, and planning complexity.
- Declare implicit invocation and document persistent user-level gateway-first image routing.
- Add offline request and configuration regression tests to the installed skill.

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
