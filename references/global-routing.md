# Persistent default image routing

Skill descriptions and `allow_implicit_invocation: true` make this bridge
discoverable, but they do not impose a tool priority. Set the persistent user
preference in the executing host's `<CODEX_HOME>/AGENTS.md` and keep this bridge
enabled. Append to existing files; preserve other instructions and settings.

Suggested user-level instruction:

```text
For AI raster image generation and editing, including requests originating in
other skills, read codex-image-gen-bridge/SKILL.md from the current host's skill
directory and execute its scripts/bridge.py first. Use --config-source codex and
the calling session's Codex home/profile. Default to gpt-image-2.6, but preserve
an explicitly requested model. Keep the active provider's base URL and its own
credential paired. Other image/design skills may guide the prompt, not replace
this execution route. Use native tools or another API only when I explicitly
choose that route. A configuration or API failure must not silently change the
provider, host, model, or route. Higher-priority instructions still apply.
```

To remove a competing skill's default selection, use the real installed paths
in `<CODEX_HOME>/config.toml`:

```toml
[[skills.config]]
path = "/absolute/codex-home/skills/.system/imagegen/SKILL.md"
enabled = false

[[skills.config]]
path = "/absolute/codex-home/skills/codex-image-gen-bridge/SKILL.md"
enabled = true
```

This disables the competing skill, not native image tools themselves. Do not
patch bundled system skills: application updates may replace them. Restart
Codex when configuration changes are not reflected, and verify this bridge is
listed as `user` scope and enabled. Existing conversations may retain earlier
instructions; use a fresh turn/chat after reload.

These are host-local preferences. A server, container, alternate Codex home, or
cloud session needs its own installation and instructions; no automatic remote
synchronization or absolute tool interception is provided.

References: [Codex skills](https://learn.chatgpt.com/docs/build-skills) and
[custom instructions](https://learn.chatgpt.com/codex/agents-md).
