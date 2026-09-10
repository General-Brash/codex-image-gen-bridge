"""Offline regression tests: no real credentials, uploads, or paid API requests."""
import base64
import contextlib
import io
import http.client
import json
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zlib
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge

KEY = "unit-test-credential-not-for-real-use"


def png(width=1, height=1):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress((b"\0" + b"\x00\x55\xff" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class Response(io.BytesIO):
    def __init__(self, value, content_type="application/json", status=200, headers=None):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        super().__init__(raw)
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status = status


def http_error(status, value, headers=None):
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    return urllib.error.HTTPError("https://gateway.example/v1/images/generations", status, "test error",
                                  headers or {}, io.BytesIO(data))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / ".codex"
        self.home.mkdir()
        self.environment = patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.write_config()
        (self.home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": KEY}), encoding="utf-8")
        self.output = self.root / "output"
        self.image = self.root / "参考图.png"
        self.image.write_bytes(png())
        # Fail closed: any test that forgets its transport fixture cannot reach the network.
        self.network = patch.object(bridge.urllib.request, "build_opener", side_effect=AssertionError("Network is forbidden in offline tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write_config(self, text=None):
        (self.home / "config.toml").write_text(text or '''
model_provider = "custom"
[model_providers.unrelated]
base_url = "https://wrong.example/v1"
[model_providers.custom]
base_url = 'https://gateway.example/v1' # TOML comments must work
wire_api = "responses"
''', encoding="utf-8")

    def run_cli(self, arguments, responses=None):
        out, err = io.StringIO(), io.StringIO()
        opener = Mock()
        if responses is not None:
            opener.open.side_effect = responses
            transport = patch.object(bridge.urllib.request, "build_opener", return_value=opener)
        else:
            transport = contextlib.nullcontext()
        with transport, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bridge.main(["--output-dir", str(self.output), *arguments])
        return code, out.getvalue(), err.getvalue(), opener

    def success(self, arguments, responses):
        code, out, err, opener = self.run_cli(arguments, responses)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        return json.loads(out), opener

    def failure(self, arguments, responses=None):
        code, out, err, opener = self.run_cli(arguments, responses)
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")
        self.assertNotIn(KEY, err)
        self.assertNotIn("Traceback", err)
        return json.loads(err), opener

    def test_active_provider_is_not_first_url(self):
        config = bridge.load_gateway(self.home)
        self.assertEqual(config.base_url, "https://gateway.example/v1")
        self.assertEqual(config.provider, "custom")
        self.assertEqual(config.api_key, KEY)
        self.assertNotIn(KEY, repr(config))
        self.assertNotIn(KEY, json.dumps(config.summary()))

    def test_codex_home_precedence(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "configured"), "USERPROFILE": str(self.root / "profile")}):
            self.assertEqual(bridge.resolve_codex_home(), (self.root / "configured").resolve())
            self.assertEqual(bridge.resolve_codex_home(str(self.home)), self.home.resolve())
        with patch.dict(os.environ, {"USERPROFILE": str(self.root / "profile")}):
            self.assertEqual(bridge.resolve_codex_home(), (self.root / "profile" / ".codex").resolve())

    def test_ambiguous_provider_fails_closed(self):
        self.write_config('[model_providers.one]\nbase_url="https://one.example"\n[model_providers.two]\nbase_url="https://two.example"')
        with self.assertRaisesRegex(bridge.BridgeError, "ambiguous"):
            bridge.load_gateway(self.home)

    def test_single_legacy_provider_still_works(self):
        self.write_config("[model_providers.sub2api]\nbase_url='https://gateway.example'")
        self.assertEqual(bridge.load_gateway(self.home).base_url, "https://gateway.example/v1")
        self.write_config("base_url='https://gateway.example/v1'")
        self.assertEqual(bridge.load_gateway(self.home).provider, "legacy")

    def test_active_missing_base_does_not_use_other_provider(self):
        self.write_config('model_provider="empty"\n[model_providers.empty]\nwire_api="responses"\n[model_providers.other]\nbase_url="https://wrong.example"')
        with self.assertRaisesRegex(bridge.BridgeError, "no explicit base_url"):
            bridge.load_gateway(self.home)

    def test_provider_env_key_is_bound_and_required(self):
        self.write_config('model_provider="custom"\n[model_providers.custom]\nbase_url="https://gateway.example"\nenv_key="GATEWAY_TOKEN"')
        with self.assertRaisesRegex(bridge.BridgeError, "will not be substituted"):
            bridge.load_gateway(self.home)
        with patch.dict(os.environ, {"GATEWAY_TOKEN": "provider-specific-test-token"}):
            config = bridge.load_gateway(self.home)
            self.assertEqual(config.api_key, "provider-specific-test-token")
            self.assertEqual(config.key_source, "env:GATEWAY_TOKEN")

    def test_non_active_provider_cannot_reuse_current_auth(self):
        with self.assertRaisesRegex(bridge.BridgeError, "will not be reused"):
            bridge.load_gateway(self.home, "unrelated")

    def test_dedicated_environment_override_requires_pair(self):
        for pair in [{bridge.API_KEY_ENV: KEY}, {bridge.BASE_URL_ENV: "https://override.example"}]:
            with self.subTest(pair=list(pair)), patch.dict(os.environ, pair):
                with self.assertRaisesRegex(bridge.BridgeError, "together"):
                    bridge.load_gateway(self.home)
        with patch.dict(os.environ, {bridge.API_KEY_ENV: KEY, bridge.BASE_URL_ENV: "https://override.example"}):
            self.assertEqual(bridge.load_gateway(self.root / "nonexistent").base_url, "https://override.example/v1")
            with self.assertRaisesRegex(bridge.BridgeError, "--provider"):
                bridge.load_gateway(self.home, "custom")

    def test_sdk_environment_does_not_change_gateway_pair(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated-test-key", "OPENAI_BASE_URL": "https://api.openai.com/v1"}):
            config = bridge.load_gateway(self.home)
            self.assertEqual(config.api_key, KEY)
            self.assertEqual(config.base_url, "https://gateway.example/v1")

    def test_malformed_auth_and_oauth_are_not_api_credentials(self):
        for data in ["{broken", json.dumps({"tokens": {"access_token": "private-oauth-fixture"}})]:
            with self.subTest(data=data):
                (self.home / "auth.json").write_text(data, encoding="utf-8")
                error, _ = self.failure(["--dry-run", "test prompt"])
                self.assertEqual(error["error"]["kind"], "configuration")
                self.assertNotIn("private-oauth-fixture", json.dumps(error))

    def test_malformed_toml_never_prints_contents(self):
        self.write_config('model_provider = "' + KEY)
        error, _ = self.failure(["--dry-run", "prompt"])
        self.assertEqual(error["error"]["kind"], "configuration")

    def test_url_validation_and_prefix_preservation(self):
        self.assertEqual(bridge.normalize_base_url("https://gateway.example/proxy/v1/"), "https://gateway.example/proxy/v1")
        self.assertEqual(bridge.normalize_base_url("http://127.0.0.1:8080/api"), "http://127.0.0.1:8080/api/v1")
        for value in ["file:///tmp/a", "https://user:pass@gateway.example", "https://gateway.example?token=private",
                      "https://gateway.example/#key", "https://gateway.example/v1/images/edits", "https://gateway.example:bad"]:
            with self.subTest(url=value), self.assertRaises(bridge.BridgeError):
                bridge.normalize_base_url(value)

    def test_dimensions_preserve_explicit_size_and_real_ratios(self):
        for requested, expected in [("2048x1152", "2048x1152"), ("1152X2048", "1152x2048"), ("auto", "auto")]:
            self.assertEqual(bridge.map_size(requested), expected)
        for ratio in bridge.SIZE_ALIASES:
            width, height = map(int, bridge.map_size(ratio).split("x"))
            left, right = map(int, ratio.split(":"))
            self.assertEqual(width * right, height * left, ratio)
        self.assertEqual(bridge.map_size("9:16"), "1152x2048")
        for bad in ["unknown", "2k", "0x1024", "-1x1", "100000x100000", "1:2"]:
            with self.subTest(size=bad), self.assertRaises(bridge.BridgeError):
                bridge.map_size(bad)

    def test_dry_run_is_offline_and_does_not_expose_prompt_or_key(self):
        code, out, err, _ = self.run_cli(["--dry-run", "--size", "9:16", "private prompt not for logging"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["network_requests"], 0)
        self.assertEqual(data["requested_size"], "1152x2048")
        self.assertNotIn("private prompt", out)
        self.assertNotIn(KEY, out)
        self.assertFalse(self.output.exists())

    def test_prompt_file_is_used_as_utf8_and_exclusive(self):
        path = self.root / "prompt.txt"
        path.write_text("\ufeff绘制一张蓝色圆形图片", encoding="utf-8")
        data, opener = self.success(["--prompt-file", str(path)], [Response(png(), "image/png")])
        sent = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(sent["prompt"], "绘制一张蓝色圆形图片")
        self.assertEqual(data["status"], "ok")
        error, _ = self.failure(["--prompt-file", str(path), "also positional"])
        self.assertEqual(error["error"]["kind"], "validation")

    def test_input_modes_cannot_silently_drop_references(self):
        for arguments in [["--mode", "edit", "prompt"], ["--image", str(self.image), "prompt"], [""],
                          ["--diagnose", "prompt"], ["--timeout", "nan", "prompt"], ["--size", "invalid", "prompt"]]:
            with self.subTest(arguments=arguments):
                error, _ = self.failure(arguments)
                self.assertEqual(error["error"]["kind"], "validation")

    def test_generate_request_keeps_host_key_headers_and_pixels(self):
        data, opener = self.success(["--size", "2048x1152", "--quality", "high", "prompt"], [Response(png(), "image/png")])
        request = opener.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, "https://gateway.example/v1/images/generations")
        self.assertEqual(headers["authorization"], "Bearer " + KEY)
        self.assertEqual(headers["user-agent"], bridge.DEFAULT_USER_AGENT)
        self.assertEqual(json.loads(request.data)["size"], "2048x1152")
        self.assertEqual(json.loads(request.data)["quality"], "high")
        self.assertEqual(data["actual_size"], "1x1")
        self.assertIs(data["size_matches_request"], False)
        self.assertTrue(data["warnings"])
        self.assertNotIn(KEY, json.dumps(data))

    def test_multi_reference_multipart_preserves_images_and_unicode_prompt(self):
        second = self.root / "参考图二.png"
        second.write_bytes(png(2, 1))
        _, opener = self.success(["--mode", "edit", "--image", str(self.image), "--image", str(second), "保留两个参考形象"],
                                 [Response(png(), "image/png")])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example/v1/images/edits")
        raw = ("Content-Type: " + request.get_header("Content-type") + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + request.data
        parts = list(BytesParser(policy=policy.default).parsebytes(raw).iter_parts())
        files = [part for part in parts if part.get_filename()]
        self.assertEqual([part.get_param("name", header="content-disposition") for part in files], ["image[]", "image[]"])
        self.assertEqual([part.get_filename() for part in files], ["reference-1.png", "reference-2.png"])
        self.assertEqual(files[0].get_payload(decode=True), png())
        self.assertEqual(files[1].get_payload(decode=True), png(2, 1))
        self.assertNotIn(str(self.root).encode(), request.data)
        self.assertIn("保留两个参考形象".encode(), request.data)

    def test_single_reference_uses_compatible_image_field(self):
        images = bridge.load_images([str(self.image)])
        self.assertEqual(images[0][0], "image")
        self.assertEqual(images[0][1], "reference-1.png")

    def test_image_type_is_detected_from_bytes(self):
        disguised = self.root / "actually-png.jpg"
        disguised.write_bytes(png())
        uploaded = bridge.load_images([str(disguised)])[0]
        self.assertEqual(uploaded[1], "reference-1.png")
        self.assertEqual(uploaded[3], "image/png")

    def test_data_url_and_image_limits(self):
        data_url = "data:image/png;base64," + base64.b64encode(png()).decode()
        self.assertEqual(bridge.load_images([data_url])[0][2], png())
        for bad in ["data:image/png;base64,not-base64!", "data:text/html;base64,YQ==", "%%%%"]:
            with self.subTest(value=bad), self.assertRaises(bridge.BridgeError):
                bridge.decode_image(bad)
        with patch.object(bridge, "MAX_IMAGE_BYTES", 10), self.assertRaises(bridge.BridgeError):
            bridge.load_images([str(self.image)])
        with self.assertRaises(bridge.BridgeError):
            bridge.load_images([str(self.image)] * 17)

    def test_url_results_are_not_downloaded_or_given_fake_dimensions(self):
        result = bridge.save_result({"data": [{"url": "https://images.example/signed.png?signature=test"}]}, self.output)
        self.assertEqual(result["actual_size"], None)
        self.assertNotIn("result", result)
        self.assertNotIn("file", result)
        self.assertFalse(self.output.exists())
        for bad in ["file:///secret", "javascript:alert(1)", "https://user:pass@images.example/image.png"]:
            with self.subTest(value=bad), self.assertRaises(bridge.BridgeError):
                bridge.save_result({"data": [{"url": bad}]}, self.output)

    def test_base64_output_is_saved_but_never_returned(self):
        encoded = base64.b64encode(png()).decode()
        source = {"data": [{"b64_json": encoded, "revised_prompt": "private prompt", "extra_token": "private token"}]}
        data, _ = self.success(["--size", "1x1", "prompt"], [Response(source)])
        self.assertEqual(Path(data["file"]).read_bytes(), png())
        self.assertTrue(data["size_matches_request"])
        for forbidden in [encoded, "b64_json", "revised_prompt", "private token", "private prompt"]:
            self.assertNotIn(forbidden, json.dumps(data))
        self.assertLess(len(json.dumps(data)), 1500)

    def test_actual_jpeg_webp_and_gif_headers(self):
        jpeg = b"\xff\xd8\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", 24, 32) + b"\x01\x01\x11\x00" + b"\xff\xd9"
        gif = b"GIF89a" + struct.pack("<HH", 32, 24) + b"\x00\x00\x00"
        webp_x = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00" * 4 + (31).to_bytes(3, "little") + (23).to_bytes(3, "little")
        packed = (31 | (23 << 14)).to_bytes(4, "little")
        webp_l = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8L" + struct.pack("<I", 10) + b"\x2f" + packed + b"\x00" * 5
        webp_v = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8 " + struct.pack("<I", 10) + b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 32, 24)
        for value, kind in [(jpeg, "image/jpeg"), (gif, "image/gif"), (webp_x, "image/webp"), (webp_l, "image/webp"), (webp_v, "image/webp")]:
            with self.subTest(kind=kind, header=value[12:16]):
                self.assertEqual(bridge.image_info(value), (kind, 32, 24))
                result = bridge.save_result({"binary": value}, self.output, "32x24")
                self.assertEqual(result["content_type"], kind)
                self.assertTrue(result["size_matches_request"])

    def test_saved_files_never_overwrite_previous_output(self):
        with patch.object(bridge.time, "time_ns", return_value=123):
            first = bridge.save_result({"binary": png()}, self.output)
            second = bridge.save_result({"binary": png(2, 1)}, self.output)
        self.assertNotEqual(first["file"], second["file"])
        self.assertEqual(Path(first["file"]).read_bytes(), png())

    def test_invalid_or_empty_success_response_is_a_failure(self):
        for response in [Response({}), Response({"data": []}), Response({"data": [{"b64_json": "bm90LWFuLWltYWdl"}]}),
                         Response(b"<html>not an image</html>", "image/png"), Response(b"not JSON"), Response([])]:
            with self.subTest(response=response):
                error, _ = self.failure(["prompt"], [response])
                self.assertIn(error["error"]["kind"], {"invalid_response", "invalid_image"})
        self.assertFalse(self.output.exists())

    def test_response_size_is_bounded(self):
        with patch.object(bridge, "MAX_RESPONSE_BYTES", 10):
            error, _ = self.failure(["prompt"], [Response(b"x" * 20)])
        self.assertEqual(error["error"]["kind"], "invalid_response")

    def test_error_body_and_request_id_are_redacted_and_actionable(self):
        body = {"error": {"code": "upstream_busy", "message": "upstream rejected " + KEY + " token; Bearer hidden-token"}}
        error, opener = self.failure(["prompt"], [http_error(503, body, {"X-Request-ID": "req-test-123"})])
        self.assertEqual(error["error"]["kind"], "upstream_unavailable")
        self.assertEqual(error["error"]["code"], "upstream_busy")
        self.assertEqual(error["error"]["request_id"], "req-test-123")
        self.assertNotIn("hidden-token", json.dumps(error))
        self.assertEqual(opener.open.call_count, 1)

    def test_html_edge_errors_keep_only_safe_short_summary(self):
        error, _ = self.failure(["prompt"], [http_error(403, b"<html><title>Request blocked</title><body>private raw html</body></html>", {"CF-Ray": "ray-test"})])
        self.assertEqual(error["error"]["kind"], "permission_or_edge_block")
        self.assertEqual(error["error"]["message"], "Request blocked")
        self.assertNotIn("private raw html", json.dumps(error))

    def test_401_does_not_switch_host_or_retry(self):
        error, opener = self.failure(["--max-retries", "2", "--fallback-model", "gpt-image-2", "--model", "preferred-model", "prompt"],
                                     [http_error(401, {"error": {"code": "invalid_api_key", "message": "bad key"}})])
        self.assertEqual(error["error"]["kind"], "authentication")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.args[0].full_url, "https://gateway.example/v1/images/generations")

    def test_size_error_is_not_model_fallback(self):
        error, opener = self.failure(["--fallback-model", "backup", "prompt"],
                                     [http_error(400, {"error": {"param": "size", "code": "invalid_value", "message": "unsupported pixels"}})])
        self.assertEqual(error["error"]["kind"], "invalid_size")
        self.assertEqual(opener.open.call_count, 1)

    def test_explicit_retries_keep_payload_and_respect_retry_after(self):
        with patch.object(bridge.time, "sleep") as sleep:
            data, opener = self.success(["--max-retries", "1", "prompt"],
                                        [http_error(429, {"error": {"message": "slow down"}}, {"Retry-After": "3"}), Response(png(), "image/png")])
        self.assertEqual(data["attempts"], 2)
        self.assertEqual(opener.open.call_args_list[0].args[0].data, opener.open.call_args_list[1].args[0].data)
        self.assertEqual(opener.open.call_args_list[0].args[0].header_items(), opener.open.call_args_list[1].args[0].header_items())
        sleep.assert_called_once_with(3)

    def test_retries_are_bounded_and_upstream_error_never_changes_model(self):
        failures = [http_error(502, {"error": {"message": "upstream down"}}) for _ in range(3)]
        with patch.object(bridge.time, "sleep"):
            error, opener = self.failure(["--max-retries", "2", "--fallback-model", "backup", "prompt"], failures)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(error["error"]["attempted_models"], ["gpt-image-2"])

    def test_model_fallback_requires_explicit_opt_in_and_structured_signal(self):
        unavailable = {"error": {"code": "model_not_found", "message": "No such model"}}
        data, opener = self.success(["--model", "requested-model", "--fallback-model", "gpt-image-2", "prompt"],
                                   [http_error(404, unavailable), Response(png(), "image/png")])
        self.assertEqual(data["attempted_models"], ["requested-model", "gpt-image-2"])
        self.assertEqual(data["model"], "gpt-image-2")
        self.assertEqual(data["attempts"], 2)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([json.loads(request.data)["model"] for request in requests], ["requested-model", "gpt-image-2"])
        self.assertEqual(requests[0].full_url, requests[1].full_url)
        error, opener = self.failure(["--model", "requested-model", "prompt"], [http_error(404, unavailable)])
        self.assertEqual(error["error"]["kind"], "model_unavailable")
        self.assertEqual(opener.open.call_count, 1)

    def test_route_404_is_not_proof_of_model_unavailability(self):
        error, opener = self.failure(["--fallback-model", "backup", "prompt"], [http_error(404, b"Not Found")])
        self.assertEqual(error["error"]["kind"], "api_error")
        self.assertEqual(opener.open.call_count, 1)

    def test_timeout_is_not_automatically_replayed_even_with_retries(self):
        error, opener = self.failure(["--max-retries", "2", "prompt"], [TimeoutError("read timed out")])
        self.assertEqual(error["error"]["kind"], "network_error")
        self.assertEqual(opener.open.call_count, 1)
        self.assertFalse(error["error"]["retryable"])

    def test_redirect_handler_refuses_credential_forwarding(self):
        request = urllib.request.Request("https://gateway.example/v1/models", headers=bridge.build_request_headers(KEY))
        with self.assertRaises(bridge.BridgeError) as raised:
            bridge.NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://unrelated.example")
        self.assertEqual(raised.exception.kind, "redirect_blocked")

    def test_diagnose_is_get_only_and_does_not_infer_generation_support(self):
        for entries, listed in [([{"id": "text-model"}], False), ([{"id": "gpt-image-2"}], True)]:
            with self.subTest(listed=listed):
                data, opener = self.success(["--diagnose"], [Response({"data": entries})])
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_method(), "GET")
                self.assertEqual(request.full_url, "https://gateway.example/v1/models")
                self.assertIsNone(request.data)
                self.assertEqual(data["model_listed"], listed)
                self.assertIs(data["image_generation_tested"], False)
                self.assertEqual(opener.open.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_generation_is_not_gated_on_model_listing(self):
        _, opener = self.success(["--model", "unlisted-but-routable", "prompt"], [Response(png(), "image/png")])
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/images/generations"))

    def test_header_override_cannot_inject_headers(self):
        with patch.dict(os.environ, {bridge.USER_AGENT_ENV: "Custom-Client/1.0"}):
            self.assertEqual(bridge.build_request_headers(KEY)["User-Agent"], "Custom-Client/1.0")
        with patch.dict(os.environ, {bridge.USER_AGENT_ENV: "Client\r\nAuthorization: attacker"}):
            error, _ = self.failure(["--dry-run", "prompt"])
            self.assertEqual(error["error"]["kind"], "configuration")

    def test_redaction_removes_long_payloads_and_obvious_credentials(self):
        source = KEY + ' sk-fake-key-123456789 Bearer hidden-token api_key=other-secret data:image/png;base64,' + 'A' * 1000
        output = bridge.redact(source, (KEY,))
        for value in [KEY, "sk-fake-key-123456789", "hidden-token", "other-secret", 'A' * 100]:
            self.assertNotIn(value, output)
        self.assertLess(len(output), 801)

    def test_retry_after_invalid_values_are_safe(self):
        self.assertEqual(bridge.retry_delay({"retry-after": "99999"}), bridge.MAX_RETRY_DELAY)
        self.assertEqual(bridge.retry_delay({"Retry-After": "-1"}), 0)
        for value in ["nan", "inf", "bad date"]:
            self.assertIsNone(bridge.retry_delay({"Retry-After": value}))

    def test_200_error_object_does_not_become_success(self):
        error, _ = self.failure(["prompt"], [Response({"error": {"message": "upstream failed", "code": "failure"}})])
        self.assertEqual(error["error"]["kind"], "api_error")
        self.assertFalse(self.output.exists())

    def test_non_object_http_error_bodies_do_not_crash(self):
        for body in [None, [], "Gateway failed", 123]:
            with self.subTest(body=body):
                error, _ = self.failure(["prompt"], [http_error(502, body)])
                self.assertEqual(error["error"]["kind"], "upstream_unavailable")

    def test_incomplete_response_stream_is_not_retried(self):
        error, opener = self.failure(["--max-retries", "2", "prompt"], [http.client.IncompleteRead(b"partial", 10)])
        self.assertEqual(error["error"]["kind"], "network_error")
        self.assertEqual(opener.open.call_count, 1)

    def test_error_echoing_prompt_does_not_expose_it(self):
        prompt = "A private description that must not appear in diagnostic output"
        error, _ = self.failure([prompt], [http_error(400, {"error": {"message": "Rejected prompt: " + prompt}})])
        self.assertNotIn(prompt, json.dumps(error))
        self.assertIn("[REDACTED]", error["error"]["message"])

    def test_image_url_cannot_expose_gateway_api_key(self):
        error, _ = self.failure(["prompt"], [Response({"data": [{"url": "https://images.example/image.png?key=" + KEY}]})])
        self.assertEqual(error["error"]["kind"], "unsafe_response")

    def test_invalid_wire_api_type_is_a_configuration_error(self):
        self.write_config('model_provider="custom"\n[model_providers.custom]\nbase_url="https://gateway.example"\nwire_api=2026-09-10')
        error, _ = self.failure(["--dry-run", "prompt"])
        self.assertEqual(error["error"]["kind"], "configuration")

    def test_version_matches_release_metadata(self):
        version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
        self.assertEqual(bridge.VERSION, version)


if __name__ == "__main__":
    unittest.main()
