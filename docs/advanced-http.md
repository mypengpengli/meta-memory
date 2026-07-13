# Advanced HTTP mode

The default is a same-machine CLI and does not start HTTP, require a token, or
require an Agent permission configuration. Use the optional
`scripts/memory_api.py` only when clients run on different devices and a shared
filesystem is unsuitable.

Start it explicitly with an agents file from
`extras/http/agents.example.json`, stored outside the repository with tokens in
environment variables. The HTTP boundary binds a token to a profile, Agent and
allowed workspace. It is not installed or configured by `meta-memory setup`.
