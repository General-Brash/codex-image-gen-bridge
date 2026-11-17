"""Offline configuration and request tests; never use live credentials or APIs."""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bridge.py"
SPEC = importlib.util.spec_from_file_location("image_bridge_under_test", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO/aZ2QAAAAASUVORK5CYII="
)
ACTIVE_CONFIG = '''model_provider = "active"
[model_providers.unrelated]
base_url = "https://unrelated.invalid/v1"
experimental_bearer_token = "unrelated-test-key"
[model_providers.active]
base_url = "https://configured.invalid/proxy/v1"
wire_api = "responses"
experimental_bearer_token = "active-test-key"
'''


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config_path = self.home / "config.toml"
        self.config_path.write_text(ACTIVE_CONFIG, encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(
            bridge.urllib.request.OpenerDirector, "open",
            side_effect=AssertionError("Live network access is forbidden in offline tests."),
        ))

    def run_cli(self, arguments, transport=None):
        out, err = io.StringIO(), io.StringIO()
        fallback = AssertionError("Unexpected network request.")
        with patch.object(bridge, "perform_request", side_effect=transport or fallback) as request:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = bridge.main(["--codex-home", str(self.home), *arguments])
        text = out.getvalue() + err.getvalue()
        for secret in ("active-test-key", "unrelated-test-key", "profile-test-key", "override-test-key"):
            self.assertNotIn(secret, text)
        return status, json.loads(out.getvalue() or err.getvalue()), request

    def capture_transport(self, captures):
        def transport(request, api_key, **options):
            self.assertEqual(api_key, "active-test-key")
            self.assertEqual(request.get_header("Authorization"), "Bearer active-test-key")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(options["max_retries"], 0)
            captures.append(request)
            return bridge.HTTPResult({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}, "test-request-id")
        return transport

    def test_generate_sends_default_model_to_active_provider(self):
        captures = []
        status, report, request = self.run_cli(
            ["--output-dir", str(self.home / "output"), "test prompt"],
            self.capture_transport(captures),
        )
        self.assertEqual(status, 0)
        request.assert_called_once()
        self.assertEqual(captures[0].full_url, "https://configured.invalid/proxy/v1/images/generations")
        payload = json.loads(captures[0].data)
        self.assertEqual(payload, {"model": "gpt-image-2.6", "prompt": "test prompt", "n": 1, "size": "1024x1024"})
        self.assertEqual(report["requested_model"], "gpt-image-2.6")
        self.assertEqual(report["model"], "gpt-image-2.6")
        self.assertEqual(report["attempted_models"], ["gpt-image-2.6"])
        self.assertEqual(report["config_source"], "codex")
        self.assertEqual(Path(report["file"]).read_bytes(), PNG)
        self.assertEqual(report["request_id"], "test-request-id")

    def test_edit_sends_default_model_and_all_references(self):
        images = [self.home / "front.png", self.home / "details.png"]
        for image in images:
            image.write_bytes(PNG)
        captures = []
        status, report, request = self.run_cli([
            "--mode", "edit", "--image", str(images[0]), "--image", str(images[1]),
            "--size", "9:16", "--quality", "max", "--complexity", "medium",
            "--output-dir", str(self.home / "output"), "edit prompt",
        ], self.capture_transport(captures))
        self.assertEqual(status, 0)
        request.assert_called_once()
        self.assertEqual(captures[0].full_url, "https://configured.invalid/proxy/v1/images/edits")
        body = captures[0].data
        for field, value in (("model", "gpt-image-2.6"), ("size", "1152x2048"), ("quality", "max"), ("complexity", "medium")):
            self.assertIn(f'name="{field}"\r\n\r\n{value}\r\n'.encode(), body)
        self.assertEqual(body.count(b'name="image[]"'), 2)
        self.assertNotIn(str(self.home).encode(), body)
        self.assertFalse(report["size_matches_request"])

    def test_explicit_models_are_not_renamed(self):
        for model in ("gpt-image-2.6-flare", "gpt-image-2.6-sunburst", "gpt-image-2", "provider-image-alias"):
            with self.subTest(model=model):
                captures = []
                status, report, _ = self.run_cli([
                    "--model", model, "--quality", "xhigh", "--complexity", "high",
                    "--output-dir", str(self.home / "output"), "test prompt",
                ], self.capture_transport(captures))
                self.assertEqual(status, 0)
                payload = json.loads(captures[0].data)
                self.assertEqual(payload["model"], model)
                self.assertEqual(payload["quality"], "xhigh")
                self.assertEqual(payload["complexity"], "high")
                self.assertEqual(report["model"], model)

    def test_dry_run_is_offline_and_creates_no_output(self):
        output = self.home / "never-created"
        status, report, request = self.run_cli(["--dry-run", "--output-dir", str(output), "test prompt"])
        self.assertEqual(status, 0)
        request.assert_not_called()
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["requested_model"], "gpt-image-2.6")
        self.assertEqual(report["key_source"], "provider:active.experimental_bearer_token")
        self.assertFalse(output.exists())

    def test_default_route_ignores_bridge_and_sdk_environment(self):
        for env in (
            {bridge.BASE_URL_ENV: "https://override.invalid/v1", bridge.API_KEY_ENV: "override-test-key"},
            {bridge.BASE_URL_ENV: "https://override.invalid/v1"},
            {"OPENAI_BASE_URL": "https://override.invalid/v1", "OPENAI_API_KEY": "override-test-key"},
        ):
            with self.subTest(env_names=list(env)), patch.dict(os.environ, env):
                status, report, request = self.run_cli(["--dry-run", "test prompt"])
                self.assertEqual(status, 0)
                self.assertEqual(report["base_url"], "https://configured.invalid/proxy/v1")
                self.assertEqual(report["provider"], "active")
                request.assert_not_called()

    def test_missing_or_invalid_codex_config_never_falls_back_to_environment(self):
        os.environ.update({bridge.BASE_URL_ENV: "https://override.invalid/v1", bridge.API_KEY_ENV: "override-test-key"})
        self.config_path.unlink()
        for content in (None, "not valid TOML = ["):
            with self.subTest(content=content):
                if content is not None:
                    self.config_path.write_text(content, encoding="utf-8")
                status, report, request = self.run_cli(["--dry-run", "test prompt"])
                self.assertEqual(status, 1)
                self.assertEqual(report["error"]["kind"], "configuration")
                request.assert_not_called()

    def test_environment_only_route_requires_explicit_selection(self):
        os.environ.update({bridge.BASE_URL_ENV: "https://override.invalid/v1", bridge.API_KEY_ENV: "override-test-key"})
        self.config_path.unlink()
        status, report, request = self.run_cli(["--config-source", "environment", "--dry-run", "test prompt"])
        self.assertEqual(status, 0)
        self.assertEqual(report["base_url"], "https://override.invalid/v1")
        self.assertEqual(report["config_source"], "environment")
        request.assert_not_called()

    def test_environment_route_rejects_partial_pair_and_provider_or_profile(self):
        for env, extra in (
            ({bridge.BASE_URL_ENV: "https://override.invalid/v1"}, []),
            ({bridge.BASE_URL_ENV: "https://override.invalid/v1", bridge.API_KEY_ENV: "override-test-key"}, ["--provider", "active"]),
            ({bridge.BASE_URL_ENV: "https://override.invalid/v1", bridge.API_KEY_ENV: "override-test-key"}, ["--profile", "review"]),
        ):
            with self.subTest(extra=extra), patch.dict(os.environ, env, clear=True):
                status, report, request = self.run_cli(["--config-source", "environment", "--dry-run", *extra, "test prompt"])
                self.assertEqual(status, 1)
                self.assertEqual(report["error"]["kind"], "configuration")
                request.assert_not_called()

    def test_provider_env_key_takes_precedence_over_other_credentials(self):
        self.config_path.write_text(ACTIVE_CONFIG + 'env_key = "ACTIVE_GATEWAY_KEY"\n', encoding="utf-8")
        os.environ["ACTIVE_GATEWAY_KEY"] = "environment-test-key"
        gateway = bridge.load_gateway(self.home)
        self.assertEqual(gateway.api_key, "environment-test-key")
        self.assertEqual(gateway.key_source, "env:ACTIVE_GATEWAY_KEY")
        self.assertNotIn(gateway.api_key, repr(gateway))

    def test_unset_provider_env_key_does_not_substitute_bearer_or_auth(self):
        self.config_path.write_text(ACTIVE_CONFIG + 'env_key = "ACTIVE_GATEWAY_KEY"\n', encoding="utf-8")
        (self.home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "auth-test-key"}), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "is unset"):
            bridge.load_gateway(self.home)

    def test_provider_bearer_does_not_read_auth_json(self):
        with patch.object(bridge, "read_api_key", side_effect=AssertionError("auth.json should not be read")):
            gateway = bridge.load_gateway(self.home)
        self.assertEqual(gateway.api_key, "active-test-key")
        self.assertNotIn(gateway.api_key, repr(gateway))
        self.assertNotIn(gateway.api_key, json.dumps(gateway.summary()))

    def test_active_provider_api_key_remains_supported_but_not_oauth_tokens(self):
        self.config_path.write_text(ACTIVE_CONFIG.replace('experimental_bearer_token = "active-test-key"\n', ""), encoding="utf-8")
        auth = self.home / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "oauth-test-token", "refresh_token": "oauth-refresh-token"}}), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "OAuth"):
            bridge.load_gateway(self.home)
        auth.write_text(json.dumps({"OPENAI_API_KEY": "auth-test-key"}), encoding="utf-8")
        gateway = bridge.load_gateway(self.home)
        self.assertEqual(gateway.api_key, "auth-test-key")
        self.assertEqual(gateway.key_source, "auth.json:API_KEY")

    def test_non_active_provider_uses_only_its_own_credential(self):
        gateway = bridge.load_gateway(self.home, "unrelated")
        self.assertEqual(gateway.base_url, "https://unrelated.invalid/v1")
        self.assertEqual(gateway.api_key, "unrelated-test-key")
        self.config_path.write_text(ACTIVE_CONFIG.replace('experimental_bearer_token = "unrelated-test-key"\n', ""), encoding="utf-8")
        (self.home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "auth-test-key"}), encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "non-active provider"):
            bridge.load_gateway(self.home, "unrelated")

    def test_invalid_provider_bearer_is_not_sent_or_substituted(self):
        for token in ("", 123, "unsafe\nheader"):
            with self.subTest(token_type=type(token).__name__):
                config = {"model_provider": "active", "model_providers": {"active": {
                    "base_url": "https://configured.invalid/v1", "experimental_bearer_token": token,
                }}}
                with patch.object(bridge, "load_codex_config", return_value=config):
                    with self.assertRaises(bridge.BridgeError):
                        bridge.load_gateway(self.home)

    def test_profile_overlays_selected_provider_and_keeps_base_config_unchanged(self):
        profile_text = '''model_provider = "profile_gateway"
[model_providers.profile_gateway]
base_url = "https://profile.invalid/v1"
experimental_bearer_token = "profile-test-key"
'''
        (self.home / "review.config.toml").write_text(profile_text, encoding="utf-8")
        status, report, request = self.run_cli(["--profile", "review", "--dry-run", "test prompt"])
        self.assertEqual(status, 0)
        self.assertEqual(report["provider"], "profile_gateway")
        self.assertEqual(report["base_url"], "https://profile.invalid/v1")
        self.assertEqual(report["codex_profile"], "review")
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), ACTIVE_CONFIG)
        request.assert_not_called()
        (self.home / "options.config.toml").write_text('[model_providers.active]\nwire_api = "chat"\n', encoding="utf-8")
        gateway = bridge.load_gateway(self.home, profile="options")
        self.assertEqual(gateway.api_key, "active-test-key")
        self.assertEqual(gateway.wire_api, "chat")

    def test_missing_or_unsafe_profile_fails_without_network(self):
        for name in ("missing", "../outside", "", "review.config.toml"):
            with self.subTest(name=name):
                status, report, request = self.run_cli(["--profile", name, "--dry-run", "test prompt"])
                self.assertEqual(status, 1)
                self.assertEqual(report["error"]["kind"], "configuration")
                request.assert_not_called()

    def test_profile_provider_change_cannot_reuse_base_auth_key(self):
        config = ACTIVE_CONFIG.replace('experimental_bearer_token = "active-test-key"\n', "")
        config = config.replace('experimental_bearer_token = "unrelated-test-key"\n', "")
        self.config_path.write_text(config, encoding="utf-8")
        (self.home / "review.config.toml").write_text('model_provider = "unrelated"\n', encoding="utf-8")
        with patch.object(bridge, "read_api_key", side_effect=AssertionError("Base auth key must not be read")):
            status, report, request = self.run_cli(["--profile", "review", "--dry-run", "test prompt"])
        self.assertEqual(status, 1)
        self.assertEqual(report["error"]["kind"], "configuration")
        self.assertIn("auth.json will not be reused", report["error"]["message"])
        request.assert_not_called()

    def test_profile_changed_api_site_cannot_inherit_old_credential(self):
        (self.home / "review.config.toml").write_text(
            '[model_providers.active]\nbase_url = "https://changed.invalid/v1"\n', encoding="utf-8",
        )
        status, report, request = self.run_cli(["--profile", "review", "--dry-run", "test prompt"])
        self.assertEqual(status, 1)
        self.assertIn("inherited credentials will not be reused", report["error"]["message"])
        request.assert_not_called()

    def test_profile_bearer_cannot_silently_lose_to_inherited_env_key(self):
        self.config_path.write_text(ACTIVE_CONFIG + 'env_key = "ACTIVE_GATEWAY_KEY"\n', encoding="utf-8")
        os.environ["ACTIVE_GATEWAY_KEY"] = "active-test-key"
        (self.home / "review.config.toml").write_text(
            '[model_providers.active]\nbase_url = "https://changed.invalid/v1"\n'
            'experimental_bearer_token = "profile-test-key"\n', encoding="utf-8",
        )
        status, report, request = self.run_cli(["--profile", "review", "--dry-run", "test prompt"])
        self.assertEqual(status, 1)
        self.assertIn("inherited env_key", report["error"]["message"])
        request.assert_not_called()

    def test_profile_site_change_accepts_explicit_matching_env_key(self):
        self.config_path.write_text(ACTIVE_CONFIG + 'env_key = "ACTIVE_GATEWAY_KEY"\n', encoding="utf-8")
        os.environ.update({"ACTIVE_GATEWAY_KEY": "active-test-key", "PROFILE_GATEWAY_KEY": "profile-test-key"})
        (self.home / "review.config.toml").write_text(
            '[model_providers.active]\nbase_url = "https://changed.invalid/v1"\n'
            'env_key = "PROFILE_GATEWAY_KEY"\n', encoding="utf-8",
        )
        gateway = bridge.load_gateway(self.home, profile="review")
        self.assertEqual(gateway.base_url, "https://changed.invalid/v1")
        self.assertEqual(gateway.api_key, "profile-test-key")
        self.assertEqual(gateway.key_source, "env:PROFILE_GATEWAY_KEY")

    def test_profile_site_change_accepts_explicit_matching_bearer(self):
        (self.home / "review.config.toml").write_text(
            '[model_providers.active]\nbase_url = "https://changed.invalid/v1"\n'
            'experimental_bearer_token = "profile-test-key"\n', encoding="utf-8",
        )
        gateway = bridge.load_gateway(self.home, profile="review")
        self.assertEqual(gateway.base_url, "https://changed.invalid/v1")
        self.assertEqual(gateway.api_key, "profile-test-key")

    def test_current_config_is_read_again_for_each_call(self):
        first = bridge.load_gateway(self.home)
        self.config_path.write_text(ACTIVE_CONFIG.replace("https://configured.invalid/proxy/v1", "https://changed.invalid/v1"), encoding="utf-8")
        second = bridge.load_gateway(self.home)
        self.assertNotEqual(first.base_url, second.base_url)
        self.assertEqual(second.base_url, "https://changed.invalid/v1")

    def test_config_home_override_has_precedence(self):
        os.environ["CODEX_HOME"] = str(self.home / "environment-home")
        self.assertEqual(bridge.resolve_codex_home(str(self.home)), self.home.resolve())
        self.assertEqual(bridge.resolve_codex_home(), (self.home / "environment-home").resolve())

    def test_model_error_does_not_downgrade_or_change_route(self):
        error = bridge.BridgeError("model_unavailable", "Requested model is unavailable.", http_status=400, code="model_not_found")
        status, report, request = self.run_cli(["test prompt"], lambda *args, **kwargs: (_ for _ in ()).throw(error))
        self.assertEqual(status, 1)
        request.assert_called_once()
        self.assertEqual(report["error"]["attempted_models"], ["gpt-image-2.6"])
        self.assertEqual(report["provider"], "active")
        self.assertEqual(report["endpoint"], "https://configured.invalid/proxy/v1/images/generations")

    def test_http_error_redacts_current_provider_credential(self):
        raw = json.dumps({"error": {"message": "Invalid active-test-key", "code": "bad_key"}}).encode()
        error = bridge.response_error(401, raw, {"x-request-id": "id-active-test-key"}, "active-test-key")
        self.assertEqual(error.kind, "authentication")
        self.assertNotIn("active-test-key", json.dumps(error.summary()))


if __name__ == "__main__":
    unittest.main()
