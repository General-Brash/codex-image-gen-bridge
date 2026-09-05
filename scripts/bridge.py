#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
)
USER_AGENT_ENV = "CODEX_IMAGE_BRIDGE_USER_AGENT"


def main() -> int:
    parser = argparse.ArgumentParser(description="Call sub2api image generation from local Codex config.")
    parser.add_argument("prompt", help="Image prompt")
    parser.add_argument("--mode", choices=["generate", "edit"], default="generate")
    parser.add_argument("--image", help="Input image path or data URL for edit mode")
    parser.add_argument("--size", default="1:1")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--output-dir", default="storage/generated-images")
    args = parser.parse_args()

    home = resolve_codex_home()
    auth_path = home / "auth.json"
    config_path = home / "config.toml"

    api_key = read_api_key(auth_path)
    base_url = read_base_url(config_path)

    if not api_key:
      raise SystemExit(f"Missing OPENAI_API_KEY in {auth_path}")
    if not base_url:
      raise SystemExit(f"Missing sub2api base_url in {config_path}")

    endpoint = f"{normalize_base_url(base_url)}/images/{'edits' if args.mode == 'edit' else 'generations'}"
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "edit":
      if not args.image:
        raise SystemExit("--image is required in edit mode")
      result = call_image_edit(endpoint, api_key, args.model, args.prompt, args.image, args.size)
    else:
      result = call_image_generate(endpoint, api_key, args.model, args.prompt, args.size)

    saved = save_result(result, output_dir)
    print(json.dumps(saved, ensure_ascii=False, indent=2))
    return 0


def resolve_codex_home() -> Path:
    userprofile = os.environ.get("USERPROFILE")
    home = os.environ.get("HOME")
    root = Path(userprofile or home or str(Path.home()))
    return root / ".codex"


def read_api_key(path: Path) -> str | None:
    if not path.exists():
      return None
    try:
      data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
      return None
    if isinstance(data, dict):
      for key in ("OPENAI_API_KEY", "openai_api_key", "api_key"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
          return value.strip()
    return None


def read_base_url(path: Path) -> str | None:
    if not path.exists():
      return None
    text = path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    in_sub2api = False
    base_url = None
    for line in lines:
      if line.startswith("[") and line.endswith("]"):
        in_sub2api = line == "[model_providers.sub2api]"
        continue
      if in_sub2api and line.startswith("base_url"):
        base_url = parse_toml_string(line)
      elif not in_sub2api and line.startswith("base_url") and base_url is None:
        base_url = parse_toml_string(line)
    return base_url


def parse_toml_string(line: str) -> str | None:
    match = re.match(r'^[A-Za-z0-9_.-]+\s*=\s*"(.*)"\s*$', line)
    if not match:
      return None
    return match.group(1)


def normalize_base_url(value: str) -> str:
    trimmed = value.strip().rstrip("/")
    return trimmed if trimmed.endswith("/v1") else f"{trimmed}/v1"


def get_user_agent() -> str:
    configured = os.environ.get(USER_AGENT_ENV, "").strip()
    return configured or DEFAULT_USER_AGENT


def build_request_headers(api_key: str, content_type: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
        "Accept": "application/json",
        "User-Agent": get_user_agent(),
    }


def call_image_generate(endpoint: str, api_key: str, model: str, prompt: str, size: str):
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": map_size(size),
    }
    return post_json(endpoint, api_key, payload)


def call_image_edit(endpoint: str, api_key: str, model: str, prompt: str, image: str, size: str):
    if image.startswith("data:image/"):
      image_bytes, filename, content_type = data_url_to_upload(image)
      fields = {
          "model": model,
          "prompt": prompt,
          "n": "1",
          "size": map_size(size),
      }
      files = [("image", filename, image_bytes, content_type)]
      return post_multipart(endpoint, api_key, fields, files)

    image_path = Path(image).expanduser()
    if not image_path.exists():
      raise SystemExit(f"Image path does not exist: {image_path}")
    content_type = detect_content_type(image_path.suffix.lower())
    fields = {
        "model": model,
        "prompt": prompt,
        "n": "1",
        "size": map_size(size),
    }
    files = [("image", image_path.name, image_path.read_bytes(), content_type)]
    return post_multipart(endpoint, api_key, fields, files)


def post_json(endpoint: str, api_key: str, payload: dict):
    request = urllib.request.Request(
        endpoint,
        method="POST",
        headers=build_request_headers(api_key, "application/json"),
        data=json.dumps(payload).encode("utf-8"),
    )
    with urllib.request.urlopen(request, timeout=120) as response:
      return read_response(response)


def post_multipart(endpoint: str, api_key: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]):
    boundary = f"----codexbridge{int(time.time() * 1000)}"
    body = build_multipart_body(boundary, fields, files)
    request = urllib.request.Request(
        endpoint,
        method="POST",
        headers=build_request_headers(api_key, f"multipart/form-data; boundary={boundary}"),
        data=body,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
      return read_response(response)


def build_multipart_body(boundary: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> bytes:
    lines: list[bytes] = []
    boundary_bytes = boundary.encode("utf-8")
    for key, value in fields.items():
      lines.extend([
          b"--" + boundary_bytes,
          f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"),
          b"",
          value.encode("utf-8"),
      ])
    for field_name, filename, data, content_type in files:
      lines.extend([
          b"--" + boundary_bytes,
          f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode("utf-8"),
          f"Content-Type: {content_type}".encode("utf-8"),
          b"",
          data,
      ])
    lines.append(b"--" + boundary_bytes + b"--")
    lines.append(b"")
    return b"\r\n".join(lines)


def read_response(response):
    raw = response.read()
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("image/"):
      return {"binary": base64.b64encode(raw).decode("ascii"), "content_type": content_type}
    try:
      return json.loads(raw.decode("utf-8"))
    except Exception:
      return {"raw": raw.decode("utf-8", errors="replace")}


def save_result(result, output_dir: Path):
    if isinstance(result, dict) and "binary" in result:
      content_type = result.get("content_type", "image/png")
      ext = detect_content_type_extension(content_type)
      data = base64.b64decode(result["binary"])
      file_name = f"codex-image-{int(time.time() * 1000)}.{ext}"
      path = output_dir / file_name
      path.write_bytes(data)
      return {"file": str(path), "content_type": content_type}

    images = []
    if isinstance(result, dict):
      images = result.get("data") or result.get("images") or []
      if isinstance(images, dict):
        images = [images]

    if not images:
      return {"result": result}

    item = images[0]
    url = item.get("url") if isinstance(item, dict) else None
    b64 = None
    if isinstance(item, dict):
      b64 = item.get("b64_json") or item.get("b64") or item.get("image")
    if url:
      return {"url": url, "result": result}
    if isinstance(b64, str) and b64:
      blob = base64.b64decode(strip_data_url(b64))
      file_name = f"codex-image-{int(time.time() * 1000)}.png"
      path = output_dir / file_name
      path.write_bytes(blob)
      return {"file": str(path), "result": result}

    return {"result": result}


def strip_data_url(value: str) -> str:
    if value.startswith("data:image/"):
      return value.split(",", 1)[1]
    return value


def data_url_to_upload(value: str):
    match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,([\s\S]+)$", value)
    if not match:
      raise SystemExit("Invalid data URL image")
    content_type = match.group(1)
    data = base64.b64decode(match.group(2))
    ext = detect_content_type_extension(content_type)
    return data, f"input.{ext}", content_type


def detect_content_type(suffix: str) -> str:
    if suffix in {".jpg", ".jpeg"}:
      return "image/jpeg"
    if suffix == ".webp":
      return "image/webp"
    if suffix == ".gif":
      return "image/gif"
    return "image/png"


def detect_content_type_extension(content_type: str) -> str:
    if "jpeg" in content_type:
      return "jpg"
    if "webp" in content_type:
      return "webp"
    if "gif" in content_type:
      return "gif"
    return "png"


def map_size(size: str) -> str:
    if size == "4:3":
      return "1536x1024"
    if size == "16:9":
      return "1536x1024"
    if size == "9:16":
      return "1024x1536"
    return "1024x1024"


if __name__ == "__main__":
    raise SystemExit(main())
