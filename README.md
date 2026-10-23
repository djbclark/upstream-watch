# upstream-watch

An off-machine backstop for the operator's local upstream watcher (`fleet-watch upstream`, which
relays each event into an agent session). Every 15 minutes a GitHub Actions run finds activity by
anyone else on djbclark's issues and pull requests in `cfengine/core`, `cfengine/libntech`,
`NorthernTechHQ/libntech` and `cfengine/buildscripts` (GitHub search, `involves:djbclark`), and
pings a Telegram topic. It only pings; it never starts work or writes anywhere upstream.

1. `watch.py`: the watcher (stdlib Python; design in its docstring).
2. `.github/workflows/upstream-watch.yml`: schedule, state in the Actions cache, keepalive.
3. Secret `TELEGRAM_BOT_TOKEN`, variable `TELEGRAM_TARGET` (`chat:thread`).

Manual runs: `gh workflow run upstream-watch.yml -R djbclark/upstream-watch -f test=true` sends one
test ping; `-f dry=true` lists what it would ping without sending or saving state.

It is public so its minutes are free; it lived in the private `djbclark/cfengine-all` for its
first hour (2026-10-23) and moved here to stop using that account's private Actions minutes.
