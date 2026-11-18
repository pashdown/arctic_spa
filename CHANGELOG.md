# Changelog

All notable changes to this project are documented here. Backfilled
2026-10-02 from GitHub release notes (https://github.com/pashdown/arctic_spa/releases)
after the local working tree was found to be git-untracked with no prior
changelog.

## [1.2.8] - 2026-11-17

Real power readings. The spa reports its total current draw (LIVE field
23, amps x100), found in packet captures: ~1.1A idle with pump 1 on low,
21.8A with all three pumps on high, ~47A briefly at pump start-up.

- New **Current** sensor (A).
- **Power** is now measured current x 240V (`SUPPLY_VOLTAGE`) when the spa
  reports current, instead of a guess from which pumps/heater are running.
  Spas that don't report it keep the estimate. A `source` attribute says
  `measured` or `estimated`.
- **Energy** accumulates from Power as before, so it becomes measured too.
  The existing total carries over; expect daily figures to shift from the
  old estimates.

## [1.2.7] - 2026-11-17

Matches the controls the Arctic Spa app offers, using packet captures of
the app talking to the spa.

- New **EZ Mode** switch (the app's EZ button): sends `ALL_ON` (command
  field 17) and reads the LIVE frame's `ALL_ON` field (24). Runs all pumps
  on high; off returns them to their previous state.
- **SpaBoy Boost** is now a button, not a switch: the app sends it as a
  one-shot `SPABOY_BOOST` (field 19) with no off command, and without the
  `SET_STEREO` suffix every other command carries (the old switch added
  the suffix and judged success from the filter status, which never
  changes). The old "Spa Boost" switch is removed.
- Entities for missing equipment are no longer created. The spa's CONFIG
  frame lists installed equipment (fields 2/3 pumps 2/3, 6 blower 1, 8
  lights, 13 Onzen, mapped against a spa with known hardware). Setup waits
  up to 5s for it; spas that don't send it keep every entity.
- The SDS switch is created disabled by default (no CONFIG field for it is
  known); enable it if your spa has SDS.
- ONZEN_LIVE frames are logged in full at DEBUG to map the remaining
  SpaBoy fields (boost timer and the like).
- Entities removed by this release (old Spa Boost switch, and on spas that
  lack them Blower 1 / Onzen) show as "no longer provided" in Home
  Assistant and can be deleted from the entity settings.

## [1.2.6] - 2026-11-17

Commands are button presses, not "set to" values. A debug log of 1.2.5
showed the spa ignoring the command value and stepping the device instead:
`SET_PUMP1=0` sent while pump 1 was LOW turned it HIGH, a second one turned
it OFF; `SET_PUMP1=2` from OFF gave LOW. Lights and pumps 2/3 toggle the
same way. Any press computed from a wrong or stale state toggled the wrong
way, which is why switches "sometimes" didn't work, and 1.2.5's re-send
after a reconnect could undo a press that had already landed.

- Requests now drive a device to the requested state one press at a time:
  press, wait for the result to settle (4s), re-read the state, press again
  if needed (pump 1 OFF->HIGH takes two presses). Nothing is sent if the
  device is already in the requested state.
- Presses are only computed from a state reported within the last 5s;
  otherwise status is requested first.
- After a stall-triggered reconnect, interrupted requests continue from the
  state the spa reports then, so a press that landed is never repeated.
- A press that changes nothing (after settling, with status flowing) is
  reported as "Spa did not respond to ..." and not retried; at most 3
  presses per request, abandoned after 60s.
- CONFIG and INFO frames are now logged at DEBUG (decoded fields and raw
  hex), to map which equipment the spa reports as installed so entities
  for missing equipment can be skipped later.

## [1.2.5] - 2026-11-17

Makes switch presses reliable across the spa's silent spells. A debug log
showed the spa going quiet for ~80s right after a burst of switch presses;
presses in that window looked like they did nothing (HA never got a status
update) or were rejected outright as "Not connected".

- Commands are tracked until a LIVE frame shows them applied. A status
  request is sent right after each command so confirmation arrives quickly.
- Stall detection: if no status at all arrives within 10s of a command, the
  session is dropped and reconnected immediately instead of waiting ~80s for
  the read timeouts. If the spa answers but the change is missing, it is
  asked once more; a fresh answer still without the change means the spa
  declined the command, which is logged and not retried.
- Unconfirmed commands are re-sent once the link is back, within a 60s
  window so an old press can't override later changes made at the spa
  panel. Commands are absolute (lights on, pump high), so a re-send of one
  that did land is harmless.
- A press while the link is down is queued for the reconnect instead of
  being dropped with an ERROR.
- Every command, confirmation, re-send and give-up is logged (DEBUG for the
  normal path, WARNING otherwise).

## [1.2.4] - 2026-11-17

Fixes stale "Connected" sessions, wrong readings for several sensors, and
phantom energy, found while chasing a spa that showed "Connected" with
stale readings and unresponsive switches.

- Stale-session watchdog: the link now counts as up only while LIVE status
  frames keep arriving. If none is parsed for `LIVE_STALE_TIMEOUT` (95s,
  three missed keepalive answers) the session is dropped and reconnected,
  even if other bytes are still trickling in. Previously any received byte
  reset the dead-link check, so a session that stopped delivering status
  stayed "Connected" indefinitely with every reading stale.
- Protobuf parsing: field keys are now decoded as varints. Fields 16 and up
  (ozone, economy, all-on, error, alarm, SDS, YESS) have two-byte keys and
  were mis-decoded (always read as 1), after which the rest of the frame was
  silently dropped. Non-varint fields are now skipped by wire type instead of
  ending the parse. Error/alarm codes that were previously hidden may now
  show up. pH/ORP from the LIVE frame are ignored once a SpaBoy frame has
  been seen, so the two sources can't fight.
- Connect handshake: the connected flag, availability grace clock and
  backoff counter are committed only after the spa answers the probe.
  Before, a spa that accepted TCP but stayed mute (typical right after a
  wifi roam) reset the backoff to 5s on every attempt (field logs showed
  ~16s retry loops for 45 minutes) and restarted the grace clock, so stale
  entities never aged out. A send failure during setup now aborts the
  attempt immediately.
- Energy: each interval is billed at the power drawn during it (previous
  state) rather than the newly arrived state, and gaps over 5 minutes
  (reconnects, outages) are skipped. Previously a reconnect after an outage
  billed the whole outage at the current power, e.g. ~19.5 kWh for 3 hours
  with the heater on. Uses a monotonic clock.
- Config flow: the validation client is always stopped. A failed setup
  attempt used to leave its reconnect supervisor running in the background
  forever, competing for the spa's single TCP slot.
- Logging: a single missed keepalive answer is now DEBUG (it recovers on
  its own and produced hundreds of warnings a day); connection resets in the
  listener and on send are WARNING instead of ERROR.
- Removed accidentally committed `__pycache__` files; added `.gitignore`.

## [1.2.3] - 2026-10-10

Extends the availability grace period from 90s to 3 hours.

- A spa whose wifi roams between access points can stay dropped for
  several minutes at a time (observed up to ~12 min in HA logbook), which
  exceeded the old 90s grace and flipped entities unavailable repeatedly
  throughout the day even though the integration was reconnecting fine.
- `AVAILABILITY_GRACE` raised to 3 hours so entities hold their last
  reading through routine wifi flakiness and only report unavailable
  after a genuine multi-hour outage. README troubleshooting section
  updated to match.

## [1.2.2] - 2026-10-07

Lowers log severity for expected, non-actionable connection failures caused
by the spa's wifi roaming between access points.

- `_connect()`'s catch-all handler was logging `OSError`s (errno 111
  connection refused, errno 113 no route to host - textbook AP-roam
  symptoms) at ERROR, which HA surfaces prominently (repair items,
  notifications) for something that isn't actionable and that the
  supervisor already retries with backoff. These now log at WARNING with
  "will retry" phrasing, consistent with the other transient-failure
  messages. Genuinely unexpected exceptions still log at ERROR.
- No behavior change to reconnect/backoff logic itself - the high log
  volume reported after 1.2.1 (hundreds of "Read timeout" / "Failed to
  connect" entries) was the existing reconnect loop working as designed
  against sustained wifi instability, not a regression.

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
