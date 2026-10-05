# Security

## Reporting a problem

Please report security problems **privately** through
[GitHub security advisories](https://github.com/hikkian/barid/security/advisories/new)
instead of a public issue. Expect a first answer within a few days.

## What Barid does and does not protect

- The panel server binds to loopback only, refuses foreign `Host` and `Origin` headers and accepts writes through a single endpoint that needs a custom header. It has **no authentication**: anything running as you on your machine can use it.
- Agent identities (`--by`) are self-declared. The board protects against **accidents** (two sessions grabbing the same GPU, an agent approving its own proposal), not against a hostile process.
- The board file `.barid/board.json` is plain JSON in your project folder. Do not put secrets in prompts, notes or report paths.
- The panel never opens files named in tasks; report paths are shown, not read.

If you find a way around one of the guarantees listed in the README ("Safety model"), that is a bug worth reporting.
