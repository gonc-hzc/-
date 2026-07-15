import argparse
import asyncio
import json

import websockets

from protocol import ProtocolError, RemoteProtocol

try:
    from bleak import BleakClient, BleakError
except ImportError:
    BleakClient = None
    BleakError = Exception


DEFAULT_BLE_ADDRESS = "19:D6:51:C4:64:57"
DEFAULT_UART_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"
WS_HOST = "127.0.0.1"
WS_PORT = 8765
STALE_TIMEOUT = 0.8
STOP_REPEAT = 3
DEFAULT_CHUNK_SIZE = 20
CHUNK_DELAY = 0.005
GUARD_INTERVAL = 1.0
RECONNECT_DELAY = 1.5
TX_LOG_PERIOD = 1.0
BLE_SEND_PERIOD = 0.06
BLE_HEARTBEAT_PERIOD = 0.18
DEFAULT_PACKET_FORMAT = "minjson"


class BleUartSink:
    def __init__(self, address, char_uuid, response, chunk_size, notify):
        if BleakClient is None:
            raise RuntimeError("bleak is not installed. Run: pip install bleak")

        self.address = address
        self.char_uuid = char_uuid
        self.response = response
        self.write_response = None
        self.chunk_size = chunk_size
        self.notify = notify
        self.notify_enabled = False
        self.client = None
        self.connected = False
        self._lock = asyncio.Lock()

    @property
    def is_connected(self):
        return self.client is not None and self.client.is_connected

    def _on_disconnect(self, _client):
        self.connected = False
        print("[BLE] disconnected")

    def _on_notify(self, _sender, data):
        text = data.decode("utf-8", errors="ignore").strip()
        if text:
            print(f"[BLE RX] {text}")
        else:
            print(f"[BLE RX] {data!r}")

    async def _connect_unlocked(self):
        if self.client is not None and self.client.is_connected:
            self.connected = True
            return

        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

        print(f"[BLE] connecting {self.address}")
        self.client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
        await self.client.connect()
        self.connected = bool(self.client.is_connected)
        print(f"[BLE] connected: {self.connected}")

        services = self.client.services
        characteristic = services.get_characteristic(self.char_uuid)
        if characteristic is None:
            await self.client.disconnect()
            self.connected = False
            self.client = None
            raise RuntimeError(f"BLE characteristic not found: {self.char_uuid}")

        print(f"[BLE] char properties: {characteristic.properties}")
        if self.response is None:
            self.write_response = "write-without-response" not in characteristic.properties
        else:
            self.write_response = self.response
        print(f"[BLE] write response: {self.write_response}")

        self.notify_enabled = False
        if self.notify and "notify" in characteristic.properties:
            await self.client.start_notify(self.char_uuid, self._on_notify)
            self.notify_enabled = True
            print("[BLE] notify enabled")

    async def connect(self):
        async with self._lock:
            await self._connect_unlocked()

    async def send(self, frame):
        data = frame.encode("utf-8")
        async with self._lock:
            if self.client is None or not self.client.is_connected:
                self.connected = False
                await self._connect_unlocked()

            if self.chunk_size <= 0:
                await self.client.write_gatt_char(
                    self.char_uuid,
                    data,
                    response=self.write_response,
                )
                return

            for offset in range(0, len(data), self.chunk_size):
                chunk = data[offset : offset + self.chunk_size]
                await self.client.write_gatt_char(
                    self.char_uuid,
                    chunk,
                    response=self.write_response,
                )
                if offset + self.chunk_size < len(data):
                    await asyncio.sleep(CHUNK_DELAY)

    async def disconnect(self):
        async with self._lock:
            if self.client is None:
                return

            try:
                if self.client.is_connected:
                    if self.notify_enabled:
                        try:
                            await self.client.stop_notify(self.char_uuid)
                        except Exception:
                            pass
                    await self.client.disconnect()
            finally:
                self.connected = False
                self.notify_enabled = False
                self.client = None

    async def close(self):
        await self.disconnect()


def build_ble_frame(protocol, control, packet_format):
    if packet_format == "compact":
        return protocol.build_compact_packet(control["throttle"], control["turn"])
    if packet_format == "minjson":
        return protocol.build_min_json_packet(control["throttle"], control["turn"])
    return protocol.build_rc_packet(control["throttle"], control["turn"])


async def send_stop(sink, protocol, packet_format="json"):
    frame = build_ble_frame(protocol, {"throttle": 0, "turn": 0}, packet_format)
    for _ in range(STOP_REPEAT):
        try:
            await sink.send(frame)
        except Exception as exc:
            print(f"[ERROR] failed to send BLE stop: {exc}")
            break
        await asyncio.sleep(0.02)


async def send_ble_status(websocket, enabled, sink, error=None):
    message = {
        "type": "ble_status",
        "enabled": enabled,
        "connected": bool(sink.is_connected),
        "error": str(error) if error else "",
    }
    try:
        await websocket.send(json.dumps(message, separators=(",", ":")))
    except websockets.ConnectionClosed:
        pass


def parse_control(protocol, data):
    if not isinstance(data, dict):
        raise ProtocolError("packet must be a JSON object")

    if "throttle" in data or "turn" in data:
        return {
            "throttle": protocol.clamp(data.get("throttle", 0)),
            "turn": protocol.clamp(data.get("turn", 0)),
        }

    vx = protocol.clamp(data.get("vx", 0), protocol.LEGACY_INPUT_MIN, protocol.LEGACY_INPUT_MAX)
    wz = protocol.clamp(data.get("wz", 0), protocol.LEGACY_INPUT_MIN, protocol.LEGACY_INPUT_MAX)
    return {
        "throttle": vx * protocol.LEGACY_SCALE,
        "turn": wz * protocol.LEGACY_SCALE,
    }


async def handle_websocket(websocket, sink, active_state, client_id, packet_format, timing):
    print("[WS] frontend connected")
    protocol = RemoteProtocol()
    stale = False
    ble_enabled = False
    guard_task = None
    tx_task = None
    ignored_disabled = False
    last_tx_log_time = 0.0
    latest_control = {"throttle": 0, "turn": 0}
    latest_control_time = 0.0
    last_sent_control = None
    last_sent_time = 0.0

    def is_active_client():
        return active_state.get("client_id") is client_id

    async def guard_ble_link():
        nonlocal ble_enabled

        while ble_enabled:
            if not is_active_client():
                return

            try:
                if not sink.is_connected:
                    await sink.connect()
                await send_ble_status(websocket, ble_enabled, sink)
                await asyncio.sleep(GUARD_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[BLE] guard reconnect pending: {exc}")
                await send_ble_status(websocket, ble_enabled, sink, exc)
                await asyncio.sleep(RECONNECT_DELAY)

    async def tx_loop():
        nonlocal last_sent_control, last_sent_time, last_tx_log_time, stale

        while ble_enabled and is_active_client():
            await asyncio.sleep(timing["send_period"])

            now = asyncio.get_running_loop().time()
            if now - latest_control_time > timing["stale_timeout"]:
                control = {"throttle": 0, "turn": 0}
                if not stale:
                    print("[SAFE] frontend stale, holding BLE stop")
                    stale = True
            else:
                control = latest_control.copy()
                if stale:
                    print("[SAFE] frontend stream restored")
                    stale = False

            changed = control != last_sent_control
            if not changed and now - last_sent_time < timing["heartbeat_period"]:
                continue

            try:
                frame = build_ble_frame(protocol, control, packet_format)
                await sink.send(frame)
                last_sent_control = control
                last_sent_time = now

                if control["throttle"] != 0 or control["turn"] != 0 or now - last_tx_log_time >= TX_LOG_PERIOD:
                    print("[BLE TX]", frame.strip())
                    last_tx_log_time = now
            except Exception as exc:
                print(f"[ERROR] BLE send failed: {exc}")
                await send_ble_status(websocket, ble_enabled, sink, exc)

    async def set_ble_enabled(enabled):
        nonlocal ble_enabled, guard_task, tx_task, ignored_disabled

        enabled = bool(enabled)
        if enabled == ble_enabled:
            await send_ble_status(websocket, ble_enabled, sink)
            return

        ble_enabled = enabled
        ignored_disabled = False

        if ble_enabled:
            print("[BLE] frontend switch on, guard enabled")
            await send_ble_status(websocket, ble_enabled, sink)
            guard_task = asyncio.create_task(guard_ble_link())
            tx_task = asyncio.create_task(tx_loop())
            return

        print("[BLE] frontend switch off, guard disabled")
        for task in (guard_task, tx_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        guard_task = None
        tx_task = None

        if sink.is_connected:
            await send_stop(sink, protocol, packet_format)
        await sink.disconnect()
        await send_ble_status(websocket, ble_enabled, sink)

    try:
        await send_ble_status(websocket, ble_enabled, sink)

        while True:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=STALE_TIMEOUT)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break

            try:
                data = json.loads(message)

                if isinstance(data, dict) and data.get("type") == "ble_control":
                    await set_ble_enabled(data.get("enabled", False))
                    continue

                if not ble_enabled:
                    if not ignored_disabled:
                        print("[BLE] remote packets ignored while frontend switch is off")
                        ignored_disabled = True
                    continue

                if not is_active_client():
                    continue

                control = parse_control(protocol, data)
                latest_control["throttle"] = control["throttle"]
                latest_control["turn"] = control["turn"]
                latest_control_time = asyncio.get_running_loop().time()
            except json.JSONDecodeError:
                print("[ERROR] invalid json:", message)
                if ble_enabled and is_active_client():
                    await send_stop(sink, protocol, packet_format)
                    print("[SAFE] invalid json, send BLE stop")
            except ProtocolError as exc:
                print(f"[ERROR] invalid packet: {exc}; message={message!r}")
                if ble_enabled and is_active_client():
                    await send_stop(sink, protocol, packet_format)
                    print("[SAFE] invalid packet, send BLE stop")
            except (BleakError, OSError, RuntimeError) as exc:
                print(f"[ERROR] BLE send failed: {exc}")
                await send_ble_status(websocket, ble_enabled, sink, exc)

    finally:
        ble_enabled = False
        for task in (guard_task, tx_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if is_active_client() and sink.is_connected:
            await send_stop(sink, protocol, packet_format)
            await sink.disconnect()
            active_state["client_id"] = None
            active_state["websocket"] = None
            print("[WS] active frontend disconnected, send BLE stop")
        else:
            print("[WS] inactive frontend disconnected")


def parse_args():
    parser = argparse.ArgumentParser(description="Web joystick BLE UART bridge for STM32 robot")
    parser.add_argument("--address", default=DEFAULT_BLE_ADDRESS, help="BLE device address")
    parser.add_argument("--char", default=DEFAULT_UART_CHAR, help="BLE UART characteristic UUID")
    parser.add_argument(
        "--response",
        dest="response",
        action="store_true",
        default=None,
        help="force BLE writes with response=True",
    )
    parser.add_argument(
        "--no-response",
        dest="response",
        action="store_false",
        help="force BLE writes with response=False",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="BLE write chunk size; use 0 to disable chunking",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="enable BLE notifications for debugging; disabled by default to reduce traffic",
    )
    parser.add_argument(
        "--packet-format",
        choices=("json", "minjson", "compact"),
        default=DEFAULT_PACKET_FORMAT,
        help="frame format sent over BLE; minjson is STM32 short JSON {v,w}; compact requires matching STM32 parser",
    )
    parser.add_argument("--host", default=WS_HOST, help="websocket bind host")
    parser.add_argument("--port", type=int, default=WS_PORT, help="websocket bind port")
    parser.add_argument(
        "--send-period",
        type=float,
        default=BLE_SEND_PERIOD,
        help="minimum BLE control send interval in seconds",
    )
    parser.add_argument(
        "--heartbeat-period",
        type=float,
        default=BLE_HEARTBEAT_PERIOD,
        help="BLE resend interval when control value is unchanged; keep below STM32 remote timeout",
    )
    parser.add_argument(
        "--stale-timeout",
        type=float,
        default=STALE_TIMEOUT,
        help="frontend silence timeout before BLE stop is held",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    sink = BleUartSink(args.address, args.char, args.response, args.chunk_size, args.notify)
    active_state = {"client_id": None, "websocket": None}
    active_lock = asyncio.Lock()
    timing = {
        "send_period": args.send_period,
        "heartbeat_period": args.heartbeat_period,
        "stale_timeout": args.stale_timeout,
    }

    try:
        async def handler(websocket):
            client_id = object()
            previous = None

            async with active_lock:
                previous = active_state.get("websocket")
                active_state["client_id"] = client_id
                active_state["websocket"] = websocket

            if previous is not None and previous is not websocket:
                try:
                    await previous.close(code=4000, reason="replaced by a newer frontend")
                except Exception:
                    pass
                print("[WS] previous frontend replaced")

            await handle_websocket(websocket, sink, active_state, client_id, args.packet_format, timing)

        async with websockets.serve(handler, args.host, args.port):
            print(f"[WS] server running: ws://{args.host}:{args.port}")
            await asyncio.Future()
    finally:
        protocol = RemoteProtocol()
        if sink.is_connected:
            await send_stop(sink, protocol, args.packet_format)
        await sink.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
