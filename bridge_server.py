import argparse
import asyncio
import json

import websockets

from protocol import ProtocolError, RemoteProtocol

try:
    import serial
except ImportError:
    serial = None


WS_HOST = "127.0.0.1"
WS_PORT = 8765
STALE_TIMEOUT = 0.8
STOP_REPEAT = 3


class SerialSink:
    def __init__(self, port, baud):
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install pyserial")
        self.ser = serial.Serial(port, baud, timeout=0.01)
        print(f"[SERIAL] opened {port} @ {baud}")

    async def send(self, frame):
        await asyncio.to_thread(self.ser.write, frame.encode("utf-8"))

    async def close(self):
        await asyncio.to_thread(self.ser.close)


class TcpSink:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        print(f"[TCP] connected {self.host}:{self.port}")

    async def send(self, frame):
        self.writer.write(frame.encode("utf-8"))
        await self.writer.drain()

    async def close(self):
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()


async def make_sink(args):
    if args.serial:
        return SerialSink(args.serial, args.baud)

    host, port_text = args.tcp.rsplit(":", 1)
    sink = TcpSink(host, int(port_text))
    await sink.connect()
    return sink


async def send_stop(sink, protocol):
    frame = protocol.build_stop_packet()
    for _ in range(STOP_REPEAT):
        try:
            await sink.send(frame)
        except Exception as exc:
            print(f"[ERROR] failed to send stop: {exc}")
            break
        await asyncio.sleep(0.02)


async def handle_websocket(websocket, args):
    print("[WS] frontend connected")
    protocol = RemoteProtocol()
    sink = await make_sink(args)
    stale = False

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=args.stale_timeout)
            except asyncio.TimeoutError:
                await sink.send(protocol.build_stop_packet())
                if not stale:
                    print("[SAFE] frontend stale, holding stop")
                    stale = True
                continue
            except websockets.ConnectionClosed:
                break

            if stale:
                print("[SAFE] frontend stream restored")
                stale = False

            try:
                data = json.loads(message)
                if isinstance(data, dict) and data.get("type") == "ble_control":
                    continue

                frame = protocol.build_from_message(data)
                await sink.send(frame)
                print("[FORWARD]", frame.strip())
            except json.JSONDecodeError:
                print("[ERROR] invalid json:", message)
                await sink.send(protocol.build_stop_packet())
                print("[SAFE] invalid json, send stop")
            except ProtocolError as exc:
                print(f"[ERROR] invalid packet: {exc}; message={message!r}")
                await sink.send(protocol.build_stop_packet())
                print("[SAFE] invalid packet, send stop")

    finally:
        await send_stop(sink, protocol)
        await sink.close()
        print("[WS] frontend disconnected, send stop")


def parse_args():
    parser = argparse.ArgumentParser(description="Web joystick bridge for STM32 robot")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--serial", metavar="COMx", help="send commands to real STM32 over serial")
    target.add_argument("--tcp", metavar="HOST:PORT", help="send commands to mock TCP server")
    parser.add_argument("--baud", type=int, default=115200, help="serial baudrate, default 115200")
    parser.add_argument("--host", default=WS_HOST, help="websocket bind host")
    parser.add_argument("--port", type=int, default=WS_PORT, help="websocket bind port")
    parser.add_argument(
        "--stale-timeout",
        type=float,
        default=STALE_TIMEOUT,
        help="frontend silence timeout before holding stop",
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    async def handler(websocket):
        await handle_websocket(websocket, args)

    async with websockets.serve(handler, args.host, args.port):
        print(f"[WS] server running: ws://{args.host}:{args.port}")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
