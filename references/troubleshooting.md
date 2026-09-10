# Troubleshooting a failed image request

Read the bridge's JSON error on stderr. Keep the `error.kind`, `http_status`,
`code`, and `request_id`; do not paste auth/config files or raw response bodies.
The body excerpt is bounded and credential/image-data redacted.

| Kind / status | Interpretation | Appropriate action |
| --- | --- | --- |
| `configuration` | Missing/ambiguous provider, missing bound key, invalid TOML/URL/header | Fix the indicated local setting; use `--dry-run` to check it without a network request. |
| `authentication` / 401 | The selected endpoint rejected the credential | Check that key and base URL belong together. A gateway key is not necessarily an OpenAI platform key. Do not try it against another host. |
| `permission_or_edge_block` / 403 | Provider permission or an edge/WAF policy rejected the request | Keep the bridge's compatible User-Agent and inspect the error/request ID. A 403 alone cannot prove the exact blocking rule. |
| `model_unavailable` | A structured `model_not_found`, `unknown_model`, `invalid_model`, `unsupported_model`, or `model_not_available` code | Report the requested identifier. An explicitly allowed `--fallback-model` may be tried once on the same gateway. |
| `invalid_size` or `invalid_request` / 400, 422 | Request rejected before generation | Use the concrete parameter error. Preserve intended ratio/resolution and model; don't silently substitute a square or a different task. |
| `rate_limited` / 429 | Rate or capacity limiting | Honor `Retry-After`; retries require an explicit small `--max-retries` budget. |
| `upstream_unavailable` / 5xx | Gateway/upstream processing failed | Use the provider error code and request ID to locate the failure. This alone does not prove a missing model, lost native tool, expired key, or bad prompt. |
| Generic 404 | The endpoint/resource was not found | Check the route; HTML `Not Found` is not a model-unavailable signal. |
| `redirect_blocked` | The authenticated request would redirect | Verify the configured canonical base URL; do not forward its credential elsewhere. |
| `network_error` | Connection, TLS, or timeout failure | Report the failure and uncertainty about server acceptance. No automatic replay, even when HTTP retries were enabled. |
| `invalid_response` / `invalid_image` | No recognizable result or invalid image data/header | The command fails instead of claiming success or dumping the response. Preserve the error for investigation. |
| `local_io` | Local file read/write failed | Fix the path or permissions; do not resend a paid request just to solve a local save error. |

## Diagnostics and retry limits

`--diagnose` makes one GET request to the selected gateway's `/models` endpoint.
A 200 proves only that the model-list request succeeded with this credential.
Some gateways omit valid image model IDs; others advertise models whose upstream
accounts are unavailable. Model enumeration is never a generation health check.

`--max-retries` defaults to `0` and accepts at most `2`. Only HTTP 429, 500, 502,
503, and 504 are retry candidates; the same endpoint, model, payload, and headers
are retained. `Retry-After` is honored up to 30 seconds. POST retries can repeat
billable work; do not enable them indiscriminately. Socket timeouts are not
retried because the server may have accepted the original request.

The default socket timeout is 120 seconds (`--timeout`, maximum 900). It is not
an end-to-end job deadline. A response returning HTTP 503 in one second is not
fixed by making this timeout longer.

An allowed fallback is independent of HTTP retry limits. It only follows an
explicit model-unavailable code and runs at most once; it never switches the
provider, protocol, or reference-edit mode.

## Result dimensions

Aliases are exact ratios: `9:16` -> `1152x2048`, `16:9` -> `2048x1152`, `4:3` ->
`1536x1152`, `3:4` -> `1152x1536`, `3:2` -> `1536x1024`, `2:3` -> `1024x1536`,
`1:1` -> `1024x1024`. Explicit pixel sizes are passed through. These mappings
are requests, not a guarantee that every gateway/model accepts those dimensions.

Successful byte/base64 results report dimensions read from PNG/JPEG/WebP/GIF
headers, plus `size_matches_request`. This is not a full image decode or visual
quality check. No automatic resize/crop is performed, and a mismatch is reported
as a warning. URL-only responses remain remote with unverified dimensions.
