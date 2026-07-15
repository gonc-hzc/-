import json
import time


class ProtocolError(ValueError):
    pass


class RemoteProtocol:
    """Build newline-delimited JSON frames understood by the STM32 firmware.

    STM32 accepts integer throttle/turn values in -1000..1000:
    The chassis firmware negates both command channels before wheel mixing, so
    command signs are:
    - throttle: negative forward, positive backward
    - turn: positive left turn, negative right turn
    - BLE short JSON uses STM32's v/w parser: {"v": throttle, "w": turn}
    """

    INPUT_MIN = -1000
    INPUT_MAX = 1000
    LEGACY_INPUT_MIN = -100
    LEGACY_INPUT_MAX = 100
    LEGACY_SCALE = 10

    def __init__(self):
        self.seq = 0

    @staticmethod
    def clamp(value, min_value=INPUT_MIN, max_value=INPUT_MAX):
        if isinstance(value, bool):
            raise ProtocolError(f"invalid numeric value: {value!r}")

        try:
            value = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProtocolError(f"invalid numeric value: {value!r}") from exc

        return max(min_value, min(max_value, value))

    def build_rc_packet(self, throttle, turn):
        throttle = self.clamp(throttle)
        turn = self.clamp(turn)

        packet = {
            "type": "rc",
            "seq": self.seq,
            "throttle": throttle,
            "turn": turn,
            "ts": round(time.time(), 3),
        }

        self.seq = (self.seq + 1) % 65536
        return json.dumps(packet, separators=(",", ":")) + "\n"

    def build_from_message(self, data):
        """Accept either the new throttle/turn protocol or old vx/wz packets."""
        if not isinstance(data, dict):
            raise ProtocolError("packet must be a JSON object")

        if "throttle" in data or "turn" in data:
            return self.build_rc_packet(data.get("throttle", 0), data.get("turn", 0))

        vx = self.clamp(data.get("vx", 0), self.LEGACY_INPUT_MIN, self.LEGACY_INPUT_MAX)
        wz = self.clamp(data.get("wz", 0), self.LEGACY_INPUT_MIN, self.LEGACY_INPUT_MAX)
        return self.build_rc_packet(vx * self.LEGACY_SCALE, wz * self.LEGACY_SCALE)

    def build_speed_packet(self, vx, wz):
        """Backward-compatible API: vx/wz -100..100 -> throttle/turn -1000..1000."""
        vx = self.clamp(vx, self.LEGACY_INPUT_MIN, self.LEGACY_INPUT_MAX)
        wz = self.clamp(wz, self.LEGACY_INPUT_MIN, self.LEGACY_INPUT_MAX)
        return self.build_rc_packet(vx * self.LEGACY_SCALE, wz * self.LEGACY_SCALE)

    def build_stop_packet(self):
        return self.build_rc_packet(0, 0)

    def build_min_json_packet(self, throttle, turn):
        throttle = self.clamp(throttle)
        turn = self.clamp(turn)
        return json.dumps({"v": throttle, "w": turn}, separators=(",", ":")) + "\n"

    def build_compact_packet(self, throttle, turn):
        throttle = self.clamp(throttle)
        turn = self.clamp(turn)
        return f"T{throttle},R{turn}\n"
