# Changelog

All notable changes to this project are documented here. Backfilled
2026-10-02 from GitHub release notes (https://github.com/pashdown/arctic_spa/releases)
after the local working tree was found to be git-untracked with no prior
changelog.

## [1.2.1] - 2026-10-02

Fixes a dead-link detection bug surfaced by HA logs: "Read timeout, checking
connection" repeating hundreds of times with no recovery.

- `_receive_loop()`'s timeout handler logged and looped forever without
  ever actually checking the connection, so a spa that went silent
  mid-session (no TCP reset, just stopped answering) left `connected` stuck
  `True` indefinitely - entities kept showing stale data and the supervisor
  never reconnected.
- Now counts consecutive read timeouts and bails out of the receive loop
  after `MAX_CONSECUTIVE_READ_TIMEOUTS` (2, ~80s of silence despite
  keepalives), letting `_supervise()`'s existing reconnect/backoff handle
  it like any other dropped link.

## [1.2.0] - 2026-08-03

Hardens connection setup: the integration now confirms the spa actually
answers on TCP 12121 before declaring the link up, and retries when it
doesn't.

- Responsiveness check on connect: after the TCP handshake to 12121,
  require a real response frame within the connect timeout. A port that
  accepts the socket but stays mute (spa asleep, wrong host, firewall) is
  treated as a failed connect, so the supervisor retries with exponential
  backoff instead of sitting "connected" with no data. The probed frame is
  not lost - it's fed to the parser.
- Setup retry: `async_setup_entry` now raises `ConfigEntryNotReady` (and
  stops the client) when the first connect fails, so Home Assistant retries
  setup with backoff instead of permanently failing the entry.
- Internal: packet framing extracted into `_ingest()` (shared by the probe
  and receive loop); `_teardown_socket()` for in-place socket cleanup.
- Manifest `documentation`/`issue_tracker`/`codeowners` now point at
  `pashdown/arctic_spa` (self-contained); riversen remains credited as
  upstream in the README.

## [1.1.0] - 2026-07-28

Backports the connection-resilience work from the 3.x rewrite to the 1.x
BlueFalls/Yoctub TCP protocol (firmware 1.x / 2.x spas).

- Exponential reconnect backoff (5/10/30/60/120s), replacing the fixed 5s
  delay and the crude 3-attempt reset.
- Availability grace: entities stay available for 90s across a brief drop,
  so a quick reconnect no longer blanks the dashboard. The "Connected"
  binary sensor still reports the raw link state.
- Supervisor refactor: the listener is now a `_supervise()` lifecycle loop
  plus a `_receive_loop()` reader, and a failed startup connect is retried
  instead of coming up permanently dead.

## [1.0.0] - unreleased date unknown (tag only, no release notes)

Initial standalone release keeping the 1.x BlueFalls/Yoctub protocol line
alive after upstream riversen/arctic_spa moved to firmware 3.x JSON
WebSocket.
