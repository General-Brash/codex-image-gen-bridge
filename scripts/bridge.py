#!/usr/bin/env python3
"""A dependency-free, provider-bound bridge to the OpenAI-compatible Images API."""
from __future__ import annotations

import argparse
import base64
import binascii
import html
import http.client
import json
import math
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Keep --help/--version usable on older interpreters.
    tomllib = None

VERSION = "1.2.0"
DEFAULT_MODEL = "gpt-image-2.6"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
)
USER_AGENT_ENV = "CODEX_IMAGE_BRIDGE_USER_AGENT"
BASE_URL_ENV = "CODEX_IMAGE_BRIDGE_BASE_URL"
API_KEY_ENV = "CODEX_IMAGE_BRIDGE_API_KEY"
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_RESPONSE_BYTES = 80 * 1024 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_RETRY_DELAY = 30.0
SIZE_ALIASES = {
    "1:1": "1024x1024",
    "4:3": "1536x1152",
    "3:4": "1152x1536",
    "16:9": "2048x1152",
    "9:16": "1152x2048",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}
MODEL_UNAVAILABLE_CODES = {
    "model_not_found", "unknown_model", "invalid_model", "unsupported_model",
    "model_not_available",
}
CONTENT_EXTENSIONS = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
}


class BridgeError(Exception):
    def __init__(self, kind: str, message: str, **details):
        super().__init__(message)
        self.kind = kind
        self.details = {key: value for key, value in details.items() if value is not None}

    def summary(self) -> dict:
        return {"kind": self.kind, "message": str(self), **self.details}


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str = field(repr=False)
    provider: str = ""
    key_source: str = ""
    wire_api: str | None = None
    config_source: str = "codex"
    profile: str | None = None

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "key_source": self.key_source,
            "codex_wire_api": self.wire_api,
            "image_api": "images",
            "config_source": self.config_source,
            "codex_profile": self.profile,
        }


@dataclass
class HTTPResult:
    data: object
    request_id: str | None = None
    attempts: int = 1


def redact(text: object, secrets=(), limit: int = 800) -> str:
    """Keep useful error text, never raw credentials or image payloads."""
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"data:image/[^\s\"']+", "[IMAGE_DATA_OMITTED]", value, flags=re.I)
    value = re.sub(r"\bsk-[A-Za-z0-9_*-]{6,}", "[REDACTED]", value)
    value = re.sub(r"(?i)Bearer\s+[^\s\"',;]+", "Bearer [REDACTED]", value)
    value = re.sub(
        r'''(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*["']?\s*[:=]\s*["']?)[^\s"',;&}]+''',
        r"\1[REDACTED]", value,
    )
    value = re.sub(r"[A-Za-z0-9+/=_-]{160,}", "[LONG_DATA_OMITTED]", value)
    value = " ".join(value.split())
    return value[:limit] + ("..." if len(value) > limit else "")


def resolve_codex_home(override: str | None = None) -> Path:
    configured = override or os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    root = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return (Path(root).expanduser() / ".codex").resolve()


def read_toml(path: Path) -> dict:
    if tomllib is None:
        raise BridgeError("configuration", "Python 3.11+ is required for TOML configuration parsing.")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise BridgeError("configuration", f"Codex config file not found: {path}") from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise BridgeError("configuration", "Invalid config.toml; check TOML syntax without sharing secrets.") from None


def merge_config_layers(base: dict, layer: dict) -> dict:
    merged = dict(base)
    for name, value in layer.items():
        if isinstance(value, dict) and isinstance(merged.get(name), dict):
            merged[name] = merge_config_layers(merged[name], value)
        else:
            merged[name] = value
    return merged


def load_codex_config(home: Path, profile: str | None = None, requested: str | None = None) -> dict:
    config = read_toml(home / "config.toml")
    if profile is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
            raise BridgeError("configuration", "Profile names must contain only letters, numbers, hyphens, and underscores.")
        layer = read_toml(home / f"{profile}.config.toml")
        merged = merge_config_layers(config, layer)
        name, entry = select_provider(merged, requested)
        original_providers = config.get("model_providers", {})
        original_entry = original_providers.get(name, {}) if isinstance(original_providers, dict) else {}
        layer_providers = layer.get("model_providers", {})
        layer_entry = layer_providers.get(name, {}) if isinstance(layer_providers, dict) else {}
        if not isinstance(original_entry, dict):
            original_entry = {}
        if not isinstance(layer_entry, dict):
            layer_entry = {}
        credential_fields = ("env_key", "experimental_bearer_token")
        if ("experimental_bearer_token" in layer_entry and "env_key" not in layer_entry
                and entry.get("env_key") is not None):
            raise BridgeError("configuration", "Profile bearer token conflicts with an inherited env_key; use a separate provider or explicitly configure its matching env_key.")
        if (original_entry.get("base_url")
                and normalize_base_url(original_entry["base_url"]) != normalize_base_url(entry["base_url"])
                and not any(field in layer_entry for field in credential_fields)):
            raise BridgeError("configuration", "Profile changes the API base URL without an explicit matching provider credential; inherited credentials will not be reused.")
        if not any(entry.get(field) is not None for field in credential_fields):
            try:
                original_name, original_active = select_provider(config)
            except BridgeError:
                original_name, original_active = None, {}
            if (name != original_name
                    or normalize_base_url(entry["base_url"]) != normalize_base_url(original_active["base_url"])):
                raise BridgeError("configuration", "Profile changes the provider or API base URL without its own credential; auth.json will not be reused.")
        config = merged
    return config


def select_provider(config: dict, requested: str | None = None) -> tuple[str, dict]:
    providers = config.get("model_providers", {})
    if not isinstance(providers, dict):
        raise BridgeError("configuration", "model_providers must be a TOML table.")
    active = config.get("model_provider")
    selected = requested or active
    if selected:
        if not isinstance(selected, str) or not isinstance(providers.get(selected), dict):
            raise BridgeError("configuration", "Selected provider is not explicitly configured; no default host will be guessed.")
        entry = providers[selected]
        if not entry.get("base_url"):
            raise BridgeError("configuration", "Selected provider has no explicit base_url; no default host will be guessed.")
        return selected, entry
    candidates = [(name, value) for name, value in providers.items()
                  if isinstance(value, dict) and value.get("base_url")]
    if config.get("base_url"):
        candidates.append(("legacy", {"base_url": config["base_url"]}))
    if len(candidates) != 1:
        raise BridgeError("configuration", "Set model_provider or --provider: provider selection is missing or ambiguous.")
    return candidates[0]


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("configuration", "base_url must be a non-empty HTTP(S) URL.")
    value = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise BridgeError("configuration", "Malformed base_url.") from None
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or re.search(r"\s", value)):
        raise BridgeError("configuration", "base_url must be HTTP(S), without credentials, query parameters, or fragments.")
    if parsed.path.endswith(("/responses", "/images/generations", "/images/edits", "/chat/completions")):
        raise BridgeError("configuration", "Set the API base URL, not an individual operation endpoint.")
    return value if parsed.path.endswith("/v1") else value + "/v1"


def read_base_url(path: Path, provider: str | None = None) -> str:
    """Compatibility helper; unlike v1.0, never picks an unrelated first base_url."""
    _, entry = select_provider(read_toml(path), provider)
    return normalize_base_url(entry["base_url"])


def read_api_key(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        raise BridgeError("configuration", "Invalid auth.json; OAuth tokens are not Images API keys.") from None
    if isinstance(data, dict):
        for name in ("OPENAI_API_KEY", "openai_api_key", "api_key"):
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def validate_header(value: str, label: str) -> str:
    if not value or any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise BridgeError("configuration", f"{label} must contain printable ASCII without control characters.")
    return value


def load_gateway(home: Path, requested: str | None = None, *, source: str = "codex", profile: str | None = None) -> GatewayConfig:
    if source == "environment":
        override_url = os.environ.get(BASE_URL_ENV, "").strip()
        override_key = os.environ.get(API_KEY_ENV, "").strip()
        if not override_url or not override_key or requested or profile is not None:
            raise BridgeError("configuration", f"Set {BASE_URL_ENV} and {API_KEY_ENV} together; do not combine environment mode with --provider or --profile.")
        return GatewayConfig(normalize_base_url(override_url), validate_header(override_key, "API key"),
                             "environment", API_KEY_ENV, config_source="environment")
    if source != "codex":
        raise BridgeError("configuration", "Config source must be codex or environment.")
    config = load_codex_config(home, profile, requested)
    name, entry = select_provider(config, requested)
    base_url = normalize_base_url(entry["base_url"])
    env_key = entry.get("env_key")
    if env_key is not None:
        if not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
            raise BridgeError("configuration", "Provider env_key must name an environment variable.")
        key = os.environ.get(env_key, "").strip()
        source = f"env:{env_key}"
        if not key:
            raise BridgeError("configuration", f"Provider credential variable {env_key} is unset; auth.json will not be substituted.")
    elif entry.get("experimental_bearer_token") is not None:
        token = entry["experimental_bearer_token"]
        if not isinstance(token, str) or not token.strip():
            raise BridgeError("configuration", "Provider experimental_bearer_token must be a non-empty string.")
        key = token.strip()
        source = f"provider:{name}.experimental_bearer_token"
    else:
        active = config.get("model_provider")
        if requested and requested != active:
            raise BridgeError("configuration", "An explicitly selected non-active provider requires its own env_key or experimental_bearer_token; active-provider credentials will not be reused.")
        key = read_api_key(home / "auth.json")
        source = "auth.json:API_KEY"
        if not key:
            raise BridgeError("configuration", "No provider API credential. Configure env_key or experimental_bearer_token, or an API key in auth.json; OAuth access/refresh tokens cannot be used as API keys.")
    wire_api = entry.get("wire_api")
    if wire_api is not None and not isinstance(wire_api, str):
        raise BridgeError("configuration", "Provider wire_api must be a string when present.")
    return GatewayConfig(base_url, validate_header(key, "API key"), name, source, wire_api, profile=profile)


def map_size(size: str) -> str:
    size = size.strip().lower()
    if size in SIZE_ALIASES:
        return SIZE_ALIASES[size]
    if size == "auto":
        return size
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
    if match and all(int(value) <= 16384 for value in match.groups()):
        return size
    raise BridgeError("validation", "Use a supported aspect ratio, auto, or explicit WIDTHxHEIGHT (positive dimensions up to 16384). No square fallback is applied.")


def get_user_agent() -> str:
    return validate_header(os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT, "User-Agent")


def build_request_headers(api_key: str, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {validate_header(api_key, 'API key')}",
        "Accept": "application/json, image/*",
        "User-Agent": get_user_agent(),
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib can otherwise forward Authorization to an unrelated redirect host.
        raise BridgeError("redirect_blocked", "Authenticated API redirects are disabled; verify the configured base_url.", http_status=code)


def header_value(headers, name: str) -> str | None:
    return next((str(value) for key, value in (headers or {}).items() if key.lower() == name.lower()), None)


def request_id(headers, key: str = "") -> str | None:
    for name in ("x-request-id", "request-id", "x-correlation-id", "cf-ray"):
        value = header_value(headers, name)
        if value:
            return redact(value, (key,), 160)
    return None


def retry_delay(headers) -> float | None:
    value = header_value(headers, "retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            seconds = (timestamp - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(MAX_RETRY_DELAY, max(0.0, seconds)) if math.isfinite(seconds) else None


def response_error(status: int, raw: bytes, headers, key: str, private_values=()) -> BridgeError:
    code = None
    param = None
    try:
        body = json.loads(raw)
        error = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            param = error.get("param")
            message = error.get("message") or body.get("message") or f"HTTP {status}"
        else:
            message = error if isinstance(error, str) and error else f"HTTP {status}"
    except (ValueError, UnicodeError):
        text = raw.decode("utf-8", errors="replace")
        title = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.S | re.I)
        message = html.unescape(re.sub(r"<[^>]+>", " ", title.group(1) if title else text)) or f"HTTP {status}"
    code = redact(code, (key,), 120) if code is not None else None
    if status in {401, 403}:
        kind = "authentication" if status == 401 else "permission_or_edge_block"
    elif code in MODEL_UNAVAILABLE_CODES:
        kind = "model_unavailable"
    elif status == 429:
        kind = "rate_limited"
    elif status >= 500:
        kind = "upstream_unavailable"
    elif status in {400, 422}:
        kind = "invalid_size" if param == "size" else "invalid_request"
    elif status in {301, 302, 303, 307, 308}:
        kind = "redirect_blocked"
    else:
        kind = "api_error"
    return BridgeError(kind, redact(message, (key, *private_values)), http_status=status, code=code,
                       request_id=request_id(headers, key), retry_after=retry_delay(headers),
                       retryable=status in {429, 500, 502, 503, 504} and kind != "model_unavailable")


def read_response(response):
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BridgeError("invalid_response", "Response exceeds the 80 MiB safety limit.")
    content_type = (header_value(response.headers, "content-type") or "").split(";", 1)[0].strip().lower()
    if content_type.startswith("image/"):
        return {"binary": raw}
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BridgeError("invalid_response", "Expected a JSON or image response; received non-JSON content.") from None
    if not isinstance(result, dict):
        raise BridgeError("invalid_response", "Expected a JSON object from the Images API.")
    return result


def perform_request(request, api_key: str, *, timeout: float = 120, max_retries: int = 0, private_values=()) -> HTTPResult:
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(max_retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                result = read_response(response)
                if isinstance(result, dict) and result.get("error") is not None:
                    raise response_error(response.status, json.dumps(result).encode(), response.headers, api_key, private_values)
                return HTTPResult(result, request_id(response.headers, api_key), attempt + 1)
        except urllib.error.HTTPError as exc:
            try:
                error = response_error(exc.code, exc.read(MAX_ERROR_BYTES), exc.headers, api_key, private_values)
            finally:
                exc.close()
            error.details["attempts"] = attempt + 1
            if not error.details.get("retryable") or attempt == max_retries:
                raise error from None
            delay = error.details.get("retry_after", min(2 ** attempt, MAX_RETRY_DELAY))
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            # A timed-out POST might already have generated/billed an image.
            raise BridgeError("network_error", redact(exc, (api_key,)), attempts=attempt + 1,
                              retryable=False, hint="Not retried: the server may already have accepted the request.") from None
    raise AssertionError("unreachable")


def post_json(endpoint: str, api_key: str, payload: dict, **options) -> HTTPResult:
    request = urllib.request.Request(endpoint, method="POST", headers=build_request_headers(api_key, "application/json"),
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return perform_request(request, api_key, private_values=(payload.get("prompt", ""),), **options)


def build_multipart_body(boundary: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> bytes:
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.extend([f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(),
                      b"", value.encode("utf-8")])
    for name, filename, data, content_type in files:
        lines.extend([f"--{boundary}".encode(),
                      f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
                      f"Content-Type: {content_type}".encode(), b"", data])
    lines.extend([f"--{boundary}--".encode(), b""])
    return b"\r\n".join(lines)


def post_multipart(endpoint: str, api_key: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]], **options) -> HTTPResult:
    boundary = "----codexbridge" + uuid.uuid4().hex
    request = urllib.request.Request(endpoint, method="POST",
                                     headers=build_request_headers(api_key, f"multipart/form-data; boundary={boundary}"),
                                     data=build_multipart_body(boundary, fields, files))
    return perform_request(request, api_key, private_values=(fields.get("prompt", ""),), **options)


def image_info(data: bytes) -> tuple[str, int, int]:
    """Read image headers without Pillow; do not claim to decode/visually verify pixels."""
    if len(data) >= 33 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return "image/png", *struct.unpack(">II", data[16:24])
    if len(data) >= 13 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif", *struct.unpack("<HH", data[6:10])
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            return "image/webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if data[12:16] == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            return "image/webp", width & 0x3FFF, height & 0x3FFF
        if data[12:16] == b"VP8L" and data[20] == 0x2F:
            packed = int.from_bytes(data[21:25], "little")
            return "image/webp", 1 + (packed & 0x3FFF), 1 + ((packed >> 14) & 0x3FFF)
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset < len(data):
            if data[offset] != 0xFF:
                break
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0xD9, 0xDA}:
                break
            if marker in range(0xD0, 0xD8) or marker == 0x01:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return "image/jpeg", width, height
            offset += length
    raise BridgeError("invalid_image", "Image bytes do not contain a supported PNG/JPEG/WebP/GIF header.")


def decode_image(value: str) -> bytes:
    if value.startswith("data:"):
        match = re.fullmatch(r"data:image/[A-Za-z0-9.+-]+;base64,([\s\S]+)", value)
        if not match:
            raise BridgeError("invalid_image", "Expected a base64 image data URL.")
        value = match.group(1)
    if len(value) > MAX_RESPONSE_BYTES:
        raise BridgeError("invalid_image", "Encoded image exceeds the response safety limit.")
    try:
        return base64.b64decode("".join(value.split()), validate=True)
    except (ValueError, binascii.Error):
        raise BridgeError("invalid_image", "Image contains invalid base64 data.") from None


def load_images(values: list[str]) -> list[tuple[str, str, bytes, str]]:
    if len(values) > 16:
        raise BridgeError("validation", "At most 16 reference images are accepted.")
    images = []
    total = 0
    for number, value in enumerate(values, 1):
        if value.startswith("data:"):
            data = decode_image(value)
        else:
            path = Path(value).expanduser()
            if not path.is_file():
                raise BridgeError("validation", f"Reference image not found: {path}")
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise BridgeError("validation", "A reference image exceeds 50 MiB.")
            data = path.read_bytes()
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > 100 * 1024 * 1024:
            raise BridgeError("validation", "Reference limits: 50 MiB each, 100 MiB total.")
        content_type, width, height = image_info(data)
        if not width or not height:
            raise BridgeError("invalid_image", "Reference image has zero dimensions.")
        # Do not put user paths, quotes, CRLF, or non-ASCII names into MIME headers.
        filename = f"reference-{number}.{CONTENT_EXTENSIONS[content_type]}"
        images.append(("image" if len(values) == 1 else "image[]", filename, data, content_type))
    return images


def call_image_generate(endpoint: str, api_key: str, model: str, prompt: str, size: str, quality: str | None = None, *, complexity: str | None = None, **options) -> HTTPResult:
    payload = {"model": model, "prompt": prompt, "n": 1, "size": map_size(size)}
    if quality:
        payload["quality"] = quality
    if complexity:
        payload["complexity"] = complexity
    return post_json(endpoint, api_key, payload, **options)


def call_image_edit(endpoint: str, api_key: str, model: str, prompt: str, images, size: str, quality: str | None = None, *, complexity: str | None = None, **options) -> HTTPResult:
    if isinstance(images, str):  # Preserve single-image helper compatibility.
        images = load_images([images])
    fields = {"model": model, "prompt": prompt, "n": "1", "size": map_size(size)}
    if quality:
        fields["quality"] = quality
    if complexity:
        fields["complexity"] = complexity
    return post_multipart(endpoint, api_key, fields, images, **options)


def save_result(result: object, output_dir: Path, requested_size: str = "auto") -> dict:
    if not isinstance(result, dict):
        raise BridgeError("invalid_response", "Response does not contain an image object.")
    blob = result.get("binary")
    if isinstance(blob, str):
        blob = decode_image(blob)
    if not isinstance(blob, bytes):
        images = result.get("data") or result.get("images")
        if isinstance(images, dict):
            images = [images]
        if not isinstance(images, list) or not images or not isinstance(images[0], dict):
            raise BridgeError("invalid_response", "API returned no usable image; raw response is intentionally not printed.")
        item = images[0]
        encoded = item.get("b64_json") or item.get("b64") or item.get("image")
        if isinstance(encoded, str) and encoded:
            blob = decode_image(encoded)
        else:
            url = item.get("url")
            try:
                parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
                if parsed is None or parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or re.search(r"\s", url):
                    raise ValueError
            except ValueError:
                raise BridgeError("invalid_response", "API returned neither valid image bytes nor an HTTP(S) image URL.") from None
            return {"url": url, "actual_size": None, "size_matches_request": None,
                    "warnings": ["URL-only result: not downloaded and pixel dimensions are unverified."]}
    content_type, width, height = image_info(blob)
    if not width or not height:
        raise BridgeError("invalid_image", "Generated image has zero dimensions.")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"codex-image-{time.time_ns()}-{uuid.uuid4().hex[:8]}.{CONTENT_EXTENSIONS[content_type]}"
    with path.open("xb") as handle:
        handle.write(blob)
    actual = f"{width}x{height}"
    matches = None if requested_size == "auto" else actual == map_size(requested_size)
    saved = {"file": str(path), "content_type": content_type, "bytes": len(blob),
             "actual_size": actual, "size_matches_request": matches}
    if matches is False:
        saved["warnings"] = [f"Gateway returned {actual}, not {map_size(requested_size)}. No resizing or cropping was performed."]
    return saved


def diagnose(config: GatewayConfig, model: str, timeout: float) -> dict:
    request = urllib.request.Request(config.base_url + "/models", method="GET", headers=build_request_headers(config.api_key))
    result = perform_request(request, config.api_key, timeout=timeout)
    entries = result.data.get("data") if isinstance(result.data, dict) else None
    if not isinstance(entries, list):
        raise BridgeError("invalid_response", "The models endpoint did not return a data list.")
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    return {"status": "diagnostic", "version": VERSION, **config.summary(), "models_endpoint": "ok",
            "models_count": len(ids), "requested_model": model, "model_listed": model in ids,
            "request_id": result.request_id, "image_generation_tested": False,
            "note": "Model listing is advisory: presence does not prove generation works; absence does not prove a gateway cannot route the model."}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("prompt", nargs="?", help="Prompt text; alternatively use --prompt-file")
    command.add_argument("--version", action="version", version=VERSION)
    command.add_argument("--prompt-file", type=Path, help="UTF-8 prompt file")
    command.add_argument("--mode", choices=["generate", "edit"], default="generate")
    command.add_argument("--image", action="append", default=[], help="Reference image path or data URL; repeat for multiple references")
    command.add_argument("--size", default="1:1", help="Aspect ratio, auto, or exact WIDTHxHEIGHT; 9:16 = 1152x2048")
    command.add_argument("--model", default=DEFAULT_MODEL, help=f"Exact gateway model ID (default: {DEFAULT_MODEL}); no alias rewriting")
    command.add_argument("--fallback-model", help="Explicit one-time fallback, only for a structured model-unavailable error")
    command.add_argument("--quality", choices=["auto", "low", "medium", "high", "xhigh", "max"])
    command.add_argument("--complexity", choices=["low", "medium", "high"], help="Optional GPT Image 2.6 planning complexity; omitted unless requested")
    command.add_argument("--output-dir", type=Path, default=Path("storage/generated-images"))
    command.add_argument("--codex-home", help="Override CODEX_HOME / ~/.codex")
    command.add_argument("--provider", help="Select a named, explicitly configured provider")
    command.add_argument("--profile", help="Use the same named Codex profile: <CODEX_HOME>/<name>.config.toml")
    command.add_argument("--config-source", choices=["codex", "environment"], default="codex",
                         help="Use Codex config by default; environment-only routing requires explicit selection")
    command.add_argument("--timeout", type=float, default=120, help="Socket timeout in seconds (default: 120)")
    command.add_argument("--max-retries", type=int, choices=range(3), default=0,
                         help="Opt into 0-2 retries on selected HTTP 429/5xx responses; POST retries may duplicate billing")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate configuration/inputs without any network request")
    mode.add_argument("--diagnose", action="store_true", help="Read-only GET /models; never generates or uploads an image")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = None
    endpoint = None
    try:
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 900:
            raise BridgeError("validation", "--timeout must be finite, greater than zero, and at most 900 seconds.")
        if not args.model.strip() or (args.fallback_model is not None and not args.fallback_model.strip()):
            raise BridgeError("validation", "Model names must not be empty.")
        size = map_size(args.size)
        config = load_gateway(resolve_codex_home(args.codex_home), args.provider, source=args.config_source, profile=args.profile)
        get_user_agent()
        if args.diagnose:
            if args.prompt or args.prompt_file or args.image or args.mode != "generate":
                raise BridgeError("validation", "--diagnose does not accept prompts, edit mode, or reference images.")
            endpoint = config.base_url + "/models"
            report = diagnose(config, args.model, min(args.timeout, 30))
        else:
            if (args.prompt is None) == (args.prompt_file is None):
                raise BridgeError("validation", "Provide exactly one positional prompt or --prompt-file.")
            prompt = args.prompt if args.prompt is not None else args.prompt_file.expanduser().read_text(encoding="utf-8-sig")
            if not prompt.strip():
                raise BridgeError("validation", "Prompt must not be empty.")
            if args.mode == "edit" and not args.image:
                raise BridgeError("validation", "Edit mode requires at least one --image.")
            if args.mode != "edit" and args.image:
                raise BridgeError("validation", "Reference images require --mode edit; they will not be silently ignored.")
            images = load_images(args.image)
            endpoint = config.base_url + ("/images/edits" if args.mode == "edit" else "/images/generations")
            summary = {"version": VERSION, **config.summary(), "endpoint": endpoint, "mode": args.mode,
                       "requested_model": args.model, "requested_size": size,
                       "requested_quality": args.quality, "requested_complexity": args.complexity}
            if args.dry_run:
                report = {"status": "dry_run", **summary, "reference_count": len(images), "prompt_characters": len(prompt),
                          "fallback_model": args.fallback_model, "max_retries": args.max_retries,
                          "network_requests": 0, "output_dir": str(args.output_dir.expanduser().resolve())}
            else:
                attempted_models = []
                request_attempts = 0
                model = args.model
                while True:
                    attempted_models.append(model)
                    options = {"timeout": args.timeout, "max_retries": args.max_retries}
                    try:
                        if args.mode == "edit":
                            response = call_image_edit(endpoint, config.api_key, model, prompt, images, size, args.quality,
                                                       complexity=args.complexity, **options)
                        else:
                            response = call_image_generate(endpoint, config.api_key, model, prompt, size, args.quality,
                                                           complexity=args.complexity, **options)
                        request_attempts += response.attempts
                        break
                    except BridgeError as exc:
                        request_attempts += exc.details.get("attempts", 1)
                        if (exc.kind == "model_unavailable" and len(attempted_models) == 1
                                and args.fallback_model and args.fallback_model != model):
                            model = args.fallback_model
                            continue
                        exc.details["attempted_models"] = attempted_models
                        exc.details["attempts"] = request_attempts
                        raise
                saved = save_result(response.data, args.output_dir, size)
                report = {"status": "ok", **summary, "model": model, "attempted_models": attempted_models,
                          "request_id": response.request_id, "attempts": request_attempts, **saved}
        if "url" in report and config.api_key in report["url"]:
            raise BridgeError("unsafe_response", "The returned image URL contains the API credential and was withheld.")
        text = json.dumps(report, ensure_ascii=False, indent=2)
        text = text.replace(json.dumps(config.api_key, ensure_ascii=False)[1:-1], "[REDACTED]")
        print(text)
        return 0
    except (BridgeError, OSError, UnicodeError) as exc:
        if not isinstance(exc, BridgeError):
            exc = BridgeError("local_io", str(exc))
        error = {"status": "error", "version": VERSION, "error": exc.summary()}
        if config:
            error.update(config.summary())
        if endpoint:
            error["endpoint"] = endpoint
        secrets = (config.api_key,) if config else (os.environ.get(API_KEY_ENV, ""),)
        # The only error output is bounded JSON; no traceback, input prompt, or raw response.
        text = json.dumps(error, ensure_ascii=False, indent=2)
        for secret in secrets:
            if secret:
                text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        print(text, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
