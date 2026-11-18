"""Arctic Spa TCP/UDP Client with persistent connection."""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .const import (
    MAGIC,
    HEADER_SIZE,
    DEFAULT_PORT,
    UDP_SEND_PORT,
    UDP_RECV_PORT,
    UDP_QUERY_MSG,
    MsgType,
    PumpStatus,
    HeaterStatus,
    FilterStatus,
    LiveField,
    CmdField,
)

_LOGGER = logging.getLogger(__name__)

# Keepalive interval in seconds
KEEPALIVE_INTERVAL = 30
# Connection timeout
CONNECT_TIMEOUT = 10
# Exponential backoff between reconnect attempts (seconds); the last value
# repeats so a spa that is powered off doesn't get hammered.
RECONNECT_DELAYS = (5, 10, 30, 60, 120)
# Short drops happen, especially over wifi (observed: a spa roaming between
# access points can stay dropped for several minutes at a time). Ride through
# a quick reconnect and only report entities unavailable once the link has
# stayed down this long.
AVAILABILITY_GRACE = 3 * 3600.0  # 3 hours
# Consecutive read timeouts (each KEEPALIVE_INTERVAL + 10s) to tolerate before
# treating the link as dead. The keepalive loop sends a LIVE request every
# KEEPALIVE_INTERVAL seconds, so staying silent this long means the spa has
# stopped answering, not just a slow response.
MAX_CONSECUTIVE_READ_TIMEOUTS = 2
# Treat the link as dead if no LIVE status frame has been parsed for this long,
# even if other bytes keep arriving. The keepalive asks for LIVE every
# KEEPALIVE_INTERVAL seconds, so this is three missed answers in a row.
# Without this, a session that still trickles some bytes but no longer
# delivers status looks "connected" forever while every reading goes stale.
LIVE_STALE_TIMEOUT = 3 * KEEPALIVE_INTERVAL + 5
# Longest gap between two status updates that energy is integrated across.
# Beyond this (a reconnect, an outage) the spa's state during the gap is
# unknown, so the gap is skipped rather than guessed at.
MAX_ENERGY_GAP = 5 * 60.0


@dataclass
class SpaStatus:
    """Spa status data."""
    
    temperature_f: int = 0
    setpoint_f: int = 0
    pump1: PumpStatus = PumpStatus.OFF
    pump2: PumpStatus = PumpStatus.OFF
    pump3: PumpStatus = PumpStatus.OFF
    pump4: PumpStatus = PumpStatus.OFF
    pump5: PumpStatus = PumpStatus.OFF
    blower1: PumpStatus = PumpStatus.OFF
    blower2: PumpStatus = PumpStatus.OFF
    lights: bool = False
    stereo: bool = False
    heater1: HeaterStatus = HeaterStatus.IDLE
    heater2: HeaterStatus = HeaterStatus.IDLE
    filter_status: FilterStatus = FilterStatus.IDLE
    onzen: bool = False
    ozone: bool = False
    economy: bool = False
    all_on: bool = False
    sds: bool = False
    yess: bool = False
    error: int = 0
    alarm: int = 0
    ph: int = 0
    orp: int = 0
    connected: bool = False
    last_update: datetime | None = None
    
    # Energy tracking
    energy_kwh: float = 0.0
    # Monotonic time and estimated power as of the previous status update.
    _last_energy_update: float | None = None
    _last_power_watts: int = 0

    # Error code descriptions (from Arctic Spas documentation)
    ERROR_CODES = {
        0: "No Error",
        1: "Temperature Sensor A Fault",
        2: "Temperature Sensor B Fault",
        3: "Water Temperature Too Hot",
        4: "Water Temperature Too Cold",
        5: "Flow Switch Error",
        6: "Dry Fire Protection",
        7: "GFCI Trip",
        8: "Heater Fault",
        9: "Communication Error",
        10: "Sensor Sync Error",
    }

    ALARM_CODES = {
        0: "No Alarm",
        1: "Water Flow Alarm",
        2: "High Temperature Alarm",
        3: "Heater Alarm",
        4: "Sensor Alarm",
    }

    @property
    def temperature_c(self) -> float:
        """Get temperature in Celsius, rounded to nearest 0.5."""
        c = (self.temperature_f - 32) * 5 / 9
        return round(c * 2) / 2  # Round to nearest 0.5

    @property
    def setpoint_c(self) -> float:
        """Get setpoint in Celsius, rounded to nearest 0.5."""
        c = (self.setpoint_f - 32) * 5 / 9
        return round(c * 2) / 2  # Round to nearest 0.5

    @property
    def heater_active(self) -> bool:
        """Check if any heater is actively heating."""
        return self.heater1 in (HeaterStatus.WARMUP, HeaterStatus.HEATING) or \
               self.heater2 in (HeaterStatus.WARMUP, HeaterStatus.HEATING)

    @property
    def filter_boost_active(self) -> bool:
        """Check if filter boost is active."""
        return self.filter_status == FilterStatus.BOOST

    @property
    def error_message(self) -> str:
        """Get human-readable error message."""
        return self.ERROR_CODES.get(self.error, f"Unknown Error ({self.error})")

    @property
    def alarm_message(self) -> str:
        """Get human-readable alarm message."""
        return self.ALARM_CODES.get(self.alarm, f"Unknown Alarm ({self.alarm})")

    @property
    def has_error(self) -> bool:
        """Check if there's an active error."""
        return self.error != 0

    @property
    def has_alarm(self) -> bool:
        """Check if there's an active alarm."""
        return self.alarm != 0

    @property
    def estimated_power_watts(self) -> int:
        """Estimate current power consumption in watts."""
        # Power constants (measured values)
        HEATER_HEATING_WATTS = 6500   # Full heating (includes Pump 1 Low)
        HEATER_WARMUP_WATTS = 350     # Heater warming up
        HEATER_COOLDOWN_WATTS = 350   # Heater cooling down
        PUMP_HIGH_WATTS = 1700        # Pump 1/2/3 on High
        PUMP_LOW_WATTS = 370          # Pump 1 Low only
        IDLE_WATTS = 15               # Electronics standby
        
        power = IDLE_WATTS
        
        # Count high-speed pumps
        high_pumps = sum([
            1 if self.pump1 == PumpStatus.HIGH else 0,
            1 if self.pump2 == PumpStatus.HIGH else 0,
            1 if self.pump3 == PumpStatus.HIGH else 0,
        ])
        
        if high_pumps > 0:
            # High power mode - heater is load-shed when pumps are on high
            power = high_pumps * PUMP_HIGH_WATTS
        elif self.heater1 == HeaterStatus.HEATING:
            # Full heating mode (includes circulation pump)
            power = HEATER_HEATING_WATTS
        elif self.heater1 == HeaterStatus.WARMUP:
            # Heater warming up
            power = HEATER_WARMUP_WATTS
        elif self.heater1 == HeaterStatus.COOLDOWN:
            # Heater cooling down
            power = HEATER_COOLDOWN_WATTS
        elif self.pump1 == PumpStatus.LOW:
            # Circulation mode only
            power = PUMP_LOW_WATTS
        
        # Add blower if running
        if self.blower1 != PumpStatus.OFF:
            power += 500  # Approximate blower power
            
        return power

    def update_energy(self) -> None:
        """Accumulate energy for the interval since the previous status update.

        Call after applying a new status. The interval is billed at the power
        the spa was drawing during it, i.e. the previous update's estimate,
        not the state that just arrived. Gaps longer than MAX_ENERGY_GAP
        (reconnects, outages) are skipped: the spa's state across them is
        unknown, and billing them at the new state's power invented kWh.
        """
        now = time.monotonic()

        if self._last_energy_update is not None:
            elapsed = now - self._last_energy_update
            if 0 < elapsed <= MAX_ENERGY_GAP:
                # kWh = kW * hours
                self.energy_kwh += (
                    self._last_power_watts / 1000.0 * elapsed / 3600.0
                )

        self._last_energy_update = now
        self._last_power_watts = self.estimated_power_watts


class ArcticSpaClient:
    """Client for communicating with Arctic Spa with persistent connection."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        """Initialize the client."""
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._status = SpaStatus()
        self._lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._running = False
        self._reconnect_attempt = 0
        self._disconnected_since: float | None = None
        self._rx_buffer = b''
        self._state_callbacks: list[Callable[[], None]] = []
        # Monotonic time of the last parsed LIVE frame, for LIVE_STALE_TIMEOUT.
        self._last_live = 0.0
        # Once a SpaBoy (ONZEN_LIVE) frame has been seen it is the only source
        # for pH/ORP, so the LIVE frame's fields can't fight it.
        self._onzen_seen = False

    @property
    def status(self) -> SpaStatus:
        """Get current spa status."""
        return self._status

    @property
    def connected(self) -> bool:
        """Check if connected."""
        return self._status.connected and self._writer is not None

    @property
    def available(self) -> bool:
        """Whether entities should still show data.

        Deliberately more forgiving than `connected`. A reconnect takes a few
        seconds, and blanking every entity for that long each time is worse
        than briefly showing the last known reading. Only report unavailable
        once the link has been down for AVAILABILITY_GRACE seconds.
        """
        if self._status.connected:
            return True
        if self._status.last_update is None or self._disconnected_since is None:
            # Never had a reading, so there is nothing worth holding on to.
            return False
        return (time.monotonic() - self._disconnected_since) < AVAILABILITY_GRACE

    def _mark_disconnected(self) -> None:
        """Record the moment the link went down, for the availability grace."""
        if self._status.connected or self._disconnected_since is None:
            self._disconnected_since = time.monotonic()
        self._status.connected = False

    def register_state_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for state changes."""
        if callback not in self._state_callbacks:
            self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: Callable[[], None]) -> None:
        """Unregister a state callback."""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def _notify_state_change(self) -> None:
        """Notify all registered callbacks of state change."""
        for callback in self._state_callbacks:
            try:
                callback()
            except Exception as err:
                _LOGGER.error("Error in state callback: %s", err)

    async def _wake_udp(self) -> bool:
        """Send UDP wake-up packet to spa."""
        loop = asyncio.get_event_loop()
        
        try:
            # Create UDP socket for sending
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            send_sock.setblocking(False)

            # Send to specific host
            await loop.sock_sendto(send_sock, UDP_QUERY_MSG, (self.host, UDP_SEND_PORT))
            _LOGGER.debug("Sent UDP wake to %s:%d", self.host, UDP_SEND_PORT)
            
            # Also send broadcast
            try:
                await loop.sock_sendto(send_sock, UDP_QUERY_MSG, ("255.255.255.255", UDP_SEND_PORT))
            except Exception:
                pass
            
            send_sock.close()

            # Try to receive response (optional, spa may not respond)
            recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            recv_sock.setblocking(False)
            
            try:
                recv_sock.bind(('', UDP_RECV_PORT))
                data = await asyncio.wait_for(
                    loop.sock_recv(recv_sock, 256),
                    timeout=2.0
                )
                _LOGGER.debug("UDP response: %s", data)
            except (asyncio.TimeoutError, OSError):
                pass
            finally:
                recv_sock.close()

            return True

        except Exception as err:
            _LOGGER.warning("UDP wake failed: %s", err)
            return False

    async def async_start(self) -> bool:
        """Start the client with persistent connection."""
        if self._running:
            return True
        
        self._running = True
        self._reconnect_attempt = 0

        success = await self._connect()
        # Start the supervisor unconditionally so a spa that is asleep or
        # briefly unreachable at startup still gets retried with backoff,
        # rather than the integration coming up permanently dead.
        self._listener_task = asyncio.create_task(self._supervise())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        return success

    async def async_stop(self) -> None:
        """Stop the client and disconnect."""
        self._running = False
        
        # Cancel background tasks
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        
        await self._disconnect()

    async def _connect(self) -> bool:
        """Establish connection to spa."""
        async with self._lock:
            try:
                # Wake up spa with UDP
                await self._wake_udp()
                await asyncio.sleep(0.5)
                
                # Connect TCP
                _LOGGER.info("Connecting to spa at %s:%d", self.host, self.port)
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=CONNECT_TIMEOUT
                )
                
                self._rx_buffer = b''
                _LOGGER.info("TCP connected to spa, waiting for a response")

                # Send initial requests to get state. A failed send means the
                # spa reset the fresh socket (seen in the field right after a
                # wifi roam) - no point waiting for a probe answer.
                for i, msg_type in enumerate((
                    MsgType.INFO,
                    MsgType.LIVE,
                    MsgType.CONFIG,
                    MsgType.ONZEN_LIVE,  # Request SpaBoy data
                )):
                    if i:
                        await asyncio.sleep(0.1)
                    if not await self._send_packet(msg_type):
                        _LOGGER.warning(
                            "Spa at %s:%d dropped the connection during "
                            "setup; will retry",
                            self.host, self.port,
                        )
                        await self._teardown_socket()
                        return False

                # Confirm the spa actually talks on this port. A TCP handshake
                # can succeed against a port that then stays mute (spa asleep,
                # wrong host, firewall, or the spa still holding a stale
                # session from before a wifi roam). Require at least one frame
                # before declaring the link up. Nothing is committed (connected
                # flag, grace clock, backoff counter) until then, so a mute
                # spa backs off properly and entities still age out.
                try:
                    probe = await asyncio.wait_for(
                        self._reader.read(4096), timeout=CONNECT_TIMEOUT
                    )
                except (asyncio.TimeoutError, OSError) as err:
                    _LOGGER.warning(
                        "No response from spa on %s:%d (%s); will retry",
                        self.host, self.port, err,
                    )
                    await self._teardown_socket()
                    return False
                if not probe:
                    _LOGGER.warning(
                        "Spa closed %s:%d without responding; will retry",
                        self.host, self.port,
                    )
                    await self._teardown_socket()
                    return False

                # The spa answered: only now is the link really up.
                self._status.connected = True
                self._disconnected_since = None
                self._reconnect_attempt = 0
                # Give the first LIVE frame a full stale window to arrive.
                self._last_live = time.monotonic()
                _LOGGER.info("Connected to spa")

                # Don't lose the probed frame - feed it to the parser.
                await self._ingest(probe)

                self._notify_state_change()
                return True

            except OSError as err:
                # Connection refused / no route to host / timed out at the
                # socket layer - expected and non-actionable when the spa's
                # wifi is roaming between access points. The supervisor
                # already retries with backoff, so this doesn't warrant
                # HA's error-level treatment (repair items, notifications).
                _LOGGER.warning(
                    "Could not reach spa at %s:%d (%s); will retry",
                    self.host, self.port, err,
                )
                await self._teardown_socket()
                return False
            except Exception as err:
                _LOGGER.error("Failed to connect to spa: %s", err)
                await self._teardown_socket()
                return False

    async def _disconnect(self) -> None:
        """Disconnect from spa."""
        async with self._lock:
            if self._writer:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
            self._reader = None
            self._writer = None
            self._mark_disconnected()
            _LOGGER.info("Disconnected from spa")

    async def _backoff_sleep(self) -> None:
        """Wait before the next reconnect attempt, with exponential backoff."""
        delay = RECONNECT_DELAYS[min(self._reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
        self._reconnect_attempt += 1
        _LOGGER.info("Reconnecting to spa in %ds (attempt %d)", delay, self._reconnect_attempt)
        await asyncio.sleep(delay)

    async def _supervise(self) -> None:
        """Listen and auto-reconnect with exponential backoff.

        Owns the connection lifecycle: (re)connects when the link is down,
        runs the receive loop while it is up, and on any drop marks the client
        disconnected, notifies entities, and backs off before retrying.
        """
        while self._running:
            if not self._status.connected or self._reader is None:
                if not await self._connect():
                    await self._backoff_sleep()
                    # Re-evaluate entities; they drop out once the grace expires.
                    self._notify_state_change()
                    continue

            try:
                await self._receive_loop()
            except asyncio.CancelledError:
                break
            except OSError as err:
                # Connection reset by peer etc. - routine on a roaming spa.
                _LOGGER.warning("Link to spa lost (%s); will reconnect", err)
            except Exception as err:
                _LOGGER.error("Error in listener loop: %s", err)

            # Link dropped. Entities keep their last reading until the grace
            # period in `available` runs out, so a quick reconnect is invisible.
            self._mark_disconnected()
            self._notify_state_change()
            await self._disconnect()
            if self._running:
                await self._backoff_sleep()
                # Notify again after the wait so entities re-evaluate; this is
                # what actually flips them unavailable once the grace expires.
                self._notify_state_change()

    async def _teardown_socket(self) -> None:
        """Close the current socket in place. Caller must hold self._lock."""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._mark_disconnected()
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _ingest(self, data: bytes) -> None:
        """Append received bytes to the buffer and dispatch complete packets."""
        buffer = self._rx_buffer + data

        while len(buffer) >= HEADER_SIZE:
            # Find magic
            if buffer[:4] != struct.pack('>I', MAGIC):
                idx = buffer.find(struct.pack('>I', MAGIC))
                if idx > 0:
                    buffer = buffer[idx:]
                else:
                    buffer = b''
                    break
                continue

            # Parse header
            _, _, _, _, pkt_type, size = struct.unpack(
                '>IIIIHH', buffer[:HEADER_SIZE]
            )

            # Check if we have full packet
            if len(buffer) < HEADER_SIZE + size:
                break

            # Extract payload
            payload = buffer[HEADER_SIZE:HEADER_SIZE + size]
            buffer = buffer[HEADER_SIZE + size:]

            # Process packet
            await self._process_packet(pkt_type, payload)

        self._rx_buffer = buffer

    async def _receive_loop(self) -> None:
        """Read and parse packets until the link drops."""
        consecutive_timeouts = 0
        while self._running and self._status.connected and self._reader is not None:
            # Read data with timeout
            try:
                data = await asyncio.wait_for(
                    self._reader.read(4096),
                    timeout=KEEPALIVE_INTERVAL + 10
                )
            except asyncio.TimeoutError:
                consecutive_timeouts += 1
                if consecutive_timeouts >= MAX_CONSECUTIVE_READ_TIMEOUTS:
                    _LOGGER.warning(
                        "No response after %d consecutive read timeouts, "
                        "treating link as dead",
                        consecutive_timeouts,
                    )
                    return
                # A single missed keepalive answer is routine for this spa
                # (hundreds per day in field logs) and recovers on its own;
                # only the dead-link decision above is worth a warning.
                _LOGGER.debug("Read timeout, waiting for next keepalive answer")
            else:
                consecutive_timeouts = 0

                if not data:
                    _LOGGER.warning("Connection closed by spa")
                    return

                await self._ingest(data)

            # Bytes arriving is not enough: the link is only useful while it
            # delivers LIVE status. Otherwise "Connected" stays on while every
            # reading is stale and commands go nowhere.
            stale = time.monotonic() - self._last_live
            if stale > LIVE_STALE_TIMEOUT:
                _LOGGER.warning(
                    "No status update from spa for %ds, treating link as dead",
                    int(stale),
                )
                return

    async def _keepalive_loop(self) -> None:
        """Background task that sends keepalive requests."""
        while self._running:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                
                if not self._status.connected:
                    continue
                
                # Send LIVE request as keepalive
                _LOGGER.debug("Sending keepalive LIVE request")
                async with self._lock:
                    if self._writer:
                        await self._send_packet(MsgType.LIVE)
                        await self._send_packet(MsgType.ONZEN_LIVE)  # Also refresh SpaBoy
                        
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Error in keepalive loop: %s", err)

    async def _process_packet(self, pkt_type: int, payload: bytes) -> None:
        """Process received packet."""
        type_name = MsgType(pkt_type).name if pkt_type in MsgType._value2member_map_ else f"TYPE_{pkt_type}"
        _LOGGER.debug("Received %s (%d bytes)", type_name, len(payload))
        
        if pkt_type == MsgType.LIVE and payload:
            self._parse_live_data(payload)
            self._last_live = time.monotonic()
            self._notify_state_change()
        elif pkt_type == MsgType.ONZEN_LIVE and payload:
            self._parse_onzen_live_data(payload)
            self._notify_state_change()

    def _make_packet(self, msg_type: int, payload: bytes = b'') -> bytes:
        """Create a packet with header."""
        return struct.pack('>IIIIHH', MAGIC, 0, 0, 0, msg_type, len(payload)) + payload

    def _encode_varint(self, value: int) -> bytes:
        """Encode a value as varint."""
        result = []
        while value > 127:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value)
        return bytes(result)

    def _encode_field(self, field_num: int, value: int) -> bytes:
        """Encode a protobuf field."""
        tag = (field_num << 3) | 0
        return self._encode_varint(tag) + self._encode_varint(value)

    def _decode_varint(self, data: bytes, offset: int) -> tuple[int, int]:
        """Decode a varint from data at offset.

        Raises ValueError if the data ends mid-varint.
        """
        value = 0
        shift = 0
        while offset < len(data):
            b = data[offset]
            offset += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                return value, offset
            shift += 7
        raise ValueError("truncated varint")

    def _iter_varint_fields(self, data: bytes):
        """Yield (field_num, value) for each varint field in a protobuf message.

        The field key (field_num << 3 | wire_type) is itself a varint, so
        fields 16 and up take two bytes. Reading it as one byte (as before
        1.2.4) mis-decoded every such field and then lost the rest of the
        frame. Non-varint fields are skipped by their wire type rather than
        ending the parse.
        """
        offset = 0
        end = len(data)
        try:
            while offset < end:
                key, offset = self._decode_varint(data, offset)
                field_num = key >> 3
                wire_type = key & 0x07
                if wire_type == 0:  # varint
                    value, offset = self._decode_varint(data, offset)
                    yield field_num, value
                elif wire_type == 1:  # 64-bit
                    offset += 8
                elif wire_type == 2:  # length-delimited
                    length, offset = self._decode_varint(data, offset)
                    offset += length
                elif wire_type == 5:  # 32-bit
                    offset += 4
                else:
                    _LOGGER.debug(
                        "Unsupported wire type %d for field %d, stopping parse",
                        wire_type, field_num,
                    )
                    return
        except ValueError as err:
            _LOGGER.debug("Truncated protobuf frame: %s", err)

    def _parse_live_data(self, data: bytes) -> None:
        """Parse live status protobuf data."""
        s = self._status
        for field_num, value in self._iter_varint_fields(data):
            if field_num == LiveField.TEMP:
                s.temperature_f = value
            elif field_num == LiveField.SETPOINT:
                s.setpoint_f = value
            elif field_num == LiveField.PUMP1:
                s.pump1 = PumpStatus(min(value, 2))
            elif field_num == LiveField.PUMP2:
                s.pump2 = PumpStatus(min(value, 2))
            elif field_num == LiveField.PUMP3:
                s.pump3 = PumpStatus(min(value, 2))
            elif field_num == LiveField.PUMP4:
                s.pump4 = PumpStatus(min(value, 2))
            elif field_num == LiveField.PUMP5:
                s.pump5 = PumpStatus(min(value, 2))
            elif field_num == LiveField.BLOWER1:
                s.blower1 = PumpStatus(min(value, 2))
            elif field_num == LiveField.BLOWER2:
                s.blower2 = PumpStatus(min(value, 2))
            elif field_num == LiveField.LIGHTS:
                s.lights = bool(value)
            elif field_num == LiveField.STEREO:
                s.stereo = bool(value)
            elif field_num == LiveField.HEATER1:
                s.heater1 = HeaterStatus(min(value, 3))
            elif field_num == LiveField.HEATER2:
                s.heater2 = HeaterStatus(min(value, 3))
            elif field_num == LiveField.FILTER:
                s.filter_status = FilterStatus(min(value, 7))
            elif field_num == LiveField.ONZEN:
                s.onzen = bool(value)
            elif field_num == LiveField.OZONE:
                s.ozone = bool(value)
            elif field_num == LiveField.ECONOMY:
                s.economy = bool(value)
            elif field_num == LiveField.ALL_ON:
                s.all_on = bool(value)
            elif field_num == LiveField.ERROR:
                s.error = value
            elif field_num == LiveField.ALARM:
                s.alarm = value
            elif field_num == LiveField.PH:
                if not self._onzen_seen:
                    s.ph = value
            elif field_num == LiveField.ORP:
                if not self._onzen_seen:
                    s.orp = value
            elif field_num == LiveField.SDS:
                s.sds = bool(value)
            elif field_num == LiveField.YESS:
                s.yess = bool(value)

        s.last_update = datetime.now()
        s.update_energy()  # Update cumulative energy tracking
        _LOGGER.debug(
            "Status: temp=%d°F, setpoint=%d°F, lights=%s, pump1=%s, heater=%s",
            s.temperature_f,
            s.setpoint_f,
            s.lights,
            s.pump1.name,
            s.heater1.name,
        )

    def _parse_onzen_live_data(self, data: bytes) -> None:
        """Parse Onzen/SpaBoy live data (Type 48) for pH and ORP."""
        for field_num, value in self._iter_varint_fields(data):
            # Field 2 = ORP (raw mV value)
            if field_num == 2:
                self._onzen_seen = True
                self._status.orp = value
                _LOGGER.debug("SpaBoy ORP: %d mV", value)
            # Field 3 = pH (divide by 100 for actual value)
            elif field_num == 3:
                self._onzen_seen = True
                self._status.ph = value
                _LOGGER.debug("SpaBoy pH raw: %d (%.2f)", value, value / 100.0)

    async def _send_packet(self, msg_type: int, payload: bytes = b'') -> bool:
        """Send a packet (must be called with lock held or from single task)."""
        if not self._writer:
            return False
        
        try:
            packet = self._make_packet(msg_type, payload)
            self._writer.write(packet)
            await self._writer.drain()
            return True
        except Exception as err:
            # Typically ECONNRESET after a wifi roam; the supervisor
            # reconnects, so this isn't error-level.
            _LOGGER.warning("Error sending packet: %s", err)
            self._mark_disconnected()
            return False

    async def async_send_command(self, payload: bytes) -> bool:
        """Send a command to the spa."""
        async with self._lock:
            if not self._status.connected:
                _LOGGER.error("Not connected to spa")
                return False
            
            return await self._send_packet(MsgType.COMMAND, payload)

    def _make_toggle_command(self, field_num: int, value: int) -> bytes:
        """Create a toggle command with stereo suffix."""
        payload = self._encode_field(field_num, value)
        payload += self._encode_field(CmdField.SET_STEREO, 1)  # Required!
        return payload

    async def async_set_lights(self, on: bool) -> bool:
        """Set lights on/off."""
        payload = self._make_toggle_command(CmdField.SET_LIGHTS, 1 if on else 0)
        return await self.async_send_command(payload)

    async def async_set_pump(self, pump_num: int, status: PumpStatus) -> bool:
        """Set pump status (1-5)."""
        field_map = {
            1: CmdField.SET_PUMP1,
            2: CmdField.SET_PUMP2,
            3: CmdField.SET_PUMP3,
            4: CmdField.SET_PUMP4,
            5: CmdField.SET_PUMP5,
        }
        if pump_num not in field_map:
            return False
        payload = self._make_toggle_command(field_map[pump_num], status.value)
        return await self.async_send_command(payload)

    async def async_set_blower(self, blower_num: int, status: PumpStatus) -> bool:
        """Set blower status (1-2)."""
        field_map = {
            1: CmdField.SET_BLOWER1,
            2: CmdField.SET_BLOWER2,
        }
        if blower_num not in field_map:
            return False
        payload = self._make_toggle_command(field_map[blower_num], status.value)
        return await self.async_send_command(payload)

    async def async_set_temperature(self, temp_f: int) -> bool:
        """Set temperature setpoint in Fahrenheit."""
        # Temperature commands don't need stereo suffix
        payload = self._encode_field(CmdField.SET_TEMP, temp_f)
        return await self.async_send_command(payload)

    async def async_set_temperature_c(self, temp_c: float) -> bool:
        """Set temperature setpoint in Celsius."""
        temp_f = round(temp_c * 9 / 5 + 32)
        return await self.async_set_temperature(temp_f)

    async def async_set_filter_boost(self, on: bool) -> bool:
        """Set filter/spa boost on/off."""
        payload = self._make_toggle_command(CmdField.SPABOY_BOOST, 1 if on else 0)
        return await self.async_send_command(payload)

    async def async_set_onzen(self, on: bool) -> bool:
        """Set Onzen on/off."""
        payload = self._make_toggle_command(CmdField.SET_ONZEN, 1 if on else 0)
        return await self.async_send_command(payload)

    async def async_set_ozone(self, on: bool) -> bool:
        """Set ozone on/off."""
        payload = self._make_toggle_command(CmdField.SET_OZONE, 1 if on else 0)
        return await self.async_send_command(payload)

    async def async_set_filter(self, on: bool) -> bool:
        """Set filter on/off."""
        payload = self._make_toggle_command(CmdField.SET_FILTER, 1 if on else 0)
        return await self.async_send_command(payload)

    async def async_set_sds(self, on: bool) -> bool:
        """Set SDS (bubbles) on/off."""
        payload = self._make_toggle_command(CmdField.SET_SDS, 1 if on else 0)
        return await self.async_send_command(payload)

    async def async_request_status(self) -> bool:
        """Request current status from spa."""
        async with self._lock:
            if not self._status.connected:
                return False
            return await self._send_packet(MsgType.LIVE)
