import json
import socket
import time


HOST = "127.0.0.1"
PORT = 9000
TIMEOUT_SEC = 0.5
INPUT_MAX = 1000


def clamp(value, min_value=-INPUT_MAX, max_value=INPUT_MAX):
    return max(min_value, min(max_value, int(value)))


def handle_packet(data):
    if "throttle" in data or "turn" in data:
        throttle = clamp(data.get("throttle", 0))
        turn = clamp(data.get("turn", 0))
    else:
        throttle = clamp(data.get("vx", 0) * 10)
        turn = clamp(data.get("wz", 0) * 10)

    # Mirror REMOTE_CONTROL_THROTTLE_SIGN / TURN_SIGN and wheel mixing in STM32.
    left = clamp(-throttle - turn)
    right = clamp(-throttle + turn)
    seq = data.get("seq", -1)

    print(
        f"[RX] seq={seq:5d} | "
        f"throttle={throttle:5d} turn={turn:5d} | "
        f"left_mix={left:5d} right_mix={right:5d}"
    )


def main():
    print(f"[INFO] mock STM32 listening on {HOST}:{PORT}")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)

    conn, addr = server.accept()
    print("[INFO] remote connected:", addr)
    conn.settimeout(0.05)

    buffer = ""
    last_rx_time = time.time()

    try:
        while True:
            try:
                chunk = conn.recv(1024)
                if not chunk:
                    print("[WARN] connection closed")
                    break

                buffer += chunk.decode("utf-8", errors="ignore")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                        last_rx_time = time.time()
                        handle_packet(data)
                    except json.JSONDecodeError:
                        print("[ERROR] invalid json:", line)

            except socket.timeout:
                if time.time() - last_rx_time > TIMEOUT_SEC:
                    print("[SAFE] timeout, motor stop")
                    last_rx_time = time.time()
    finally:
        conn.close()
        server.close()


if __name__ == "__main__":
    main()
