import argparse
import socket
import sys
import time
import msvcrt

from protocol import RemoteProtocol

try:
    import serial
except ImportError:
    serial = None


SEND_PERIOD = 0.05
THROTTLE_STEP = 100
TURN_STEP = 100
INPUT_MAX = 1000


class TransportError(Exception):
    pass


class SerialTransport:
    def __init__(self, port, baud):
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install pyserial")
        self.ser = serial.Serial(port, baud, timeout=0.01)
        print(f"[SERIAL] opened {port} @ {baud}")

    def send(self, frame):
        try:
            self.ser.write(frame.encode("utf-8"))
        except Exception as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    def close(self):
        try:
            if self.ser.is_open:
                self.ser.close()
        except Exception:
            pass


class TcpTransport:
    def __init__(self, host, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        print(f"[TCP] connected {host}:{port}")

    def send(self, frame):
        try:
            self.sock.sendall(frame.encode("utf-8"))
        except Exception as exc:
            raise TransportError(f"tcp write failed: {exc}") from exc

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def clamp(value, min_value=-INPUT_MAX, max_value=INPUT_MAX):
    return max(min_value, min(max_value, int(value)))


def parse_args():
    parser = argparse.ArgumentParser(description="Keyboard software remote for STM32 robot")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--serial", metavar="COMx", help="send commands to real STM32 over serial")
    target.add_argument("--tcp", metavar="HOST:PORT", help="send commands to mock TCP server")
    parser.add_argument("--baud", type=int, default=115200, help="serial baudrate, default 115200")
    return parser.parse_args()


def open_transport(args):
    if args.serial:
        return SerialTransport(args.serial, args.baud)

    host, port_text = args.tcp.rsplit(":", 1)
    return TcpTransport(host, int(port_text))


def print_help():
    print("W/S: throttle forward/back")
    print("A/D: turn left/right")
    print("Space: stop")
    print("Q: quit")


def main():
    args = parse_args()
    protocol = RemoteProtocol()
    transport = open_transport(args)

    throttle = 0
    turn = 0
    last_send_time = 0

    print_help()

    try:
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getch().decode("utf-8", errors="ignore").lower()

                if key == "w":
                    throttle = clamp(throttle - THROTTLE_STEP)
                elif key == "s":
                    throttle = clamp(throttle + THROTTLE_STEP)
                elif key == "a":
                    turn = clamp(turn + TURN_STEP)
                elif key == "d":
                    turn = clamp(turn - TURN_STEP)
                elif key == " ":
                    throttle = 0
                    turn = 0
                elif key == "q":
                    break

            now = time.time()
            if now - last_send_time >= SEND_PERIOD:
                frame = protocol.build_rc_packet(throttle, turn)
                transport.send(frame)
                print(f"[TX] throttle={throttle:5d} turn={turn:5d} {frame.strip()}")
                last_send_time = now

            time.sleep(0.005)

    except TransportError as exc:
        print(f"[ERROR] {exc}")
        print("[SAFE] serial link is lost; STM32 should stop by timeout protection")

    finally:
        print("[SAFE] send stop")
        for _ in range(5):
            try:
                transport.send(protocol.build_stop_packet())
            except Exception:
                break
            time.sleep(0.02)
        transport.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
