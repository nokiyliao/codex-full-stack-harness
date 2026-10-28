# Codex-Compatible Local Switchboard Source

Source snapshot: d53f7e475940b39ff4f664a70eb7c4e808a2b826. License: repository AGPL-3.0-or-later.

This component supplies workflow model projection, native tool transport, capability fallback, cache/replay handling and supervised hot deployment for the local Nokiy stack. Codex retains its native thread, Goal, permission and UI ownership.

Typed intake validation rejections return correctable native-tool diagnostics without starting the host or worker. They do not report worker success or grant permissions; execution and identity checks remain in force.

Cross-generation observation preserves the original native handle only with exact dispatch provenance and paired running metadata. It is not an execution or permission grant and never launches a replacement session or replays the command.

Requires Node.js 22.15+ and Python 3.11+ for the host/tests. Install npm dependencies with npm ci and run npm test. These tests use offline fixtures; do not run live-test, model-list-smoke or service activation without your own separately configured account and authorization.

No installation capability, admission manifest, credentials, runtime state or node_modules is included. config/nokiy-workflows.json is the published workflow topology, not a complete machine installation. Source contains site-specific integration paths; portable installation is not claimed.

The operator-only external-latency diagnostic and tracked compiled Python caches were excluded. Source publication does not authorize a deployment, expose a network listener or modify the signed official App.
