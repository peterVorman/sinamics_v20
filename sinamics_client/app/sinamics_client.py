import socket
import base64
import os
import struct
import time
import logging
from pprint import pformat
from typing import Dict, List, Optional, Callable, Any

logger = logging.getLogger(__name__)


class SinamicsV20Client:
    """Minimal WebSocket client for Sinamics V20 Smart Access."""

    def __init__(self, host: str = "192.168.1.1", port: int = 80, path: str = "/"):
        self.host = host
        self.port = port
        self.path = path
        self.sock: Optional[socket.socket] = None
        # Built-in parsers. Can be extended externally if needed.
        self.parsers = {
            "r0052": parse_r0052,
            "r0020": parse_dds_float,
            "r0021": parse_dds_float,  # Actual filtered frequency
            "r0032": parse_dds_float,
            "r0035": parse_dds_float,
            "r0039": parse_dds_float,
            "r0072": parse_dds_float,  # CO: Actual output voltage [V]
            "P1080": parse_dds_float,  # Minimum frequency [Hz]
            "P1082": parse_dds_float,  # Maximum frequency [Hz]
            "r2260": parse_dds_float,  # PID setpoint after PID-RFG
            "r2294": parse_dds_float,  # Act. PID output
            "P2390": parse_dds_float,  # PID hibernation setpoint [%]
            "r4026": parse_dds_float,  # Multi-pump abs. operating hours: motor 1 [h]
            "r4027": parse_dds_float,  # Multi-pump abs. operating hours: motor 2 [h]
            "r2273": parse_dds_float,  # PID error
            "P4013": parse_dds_float,  # Multi-pump control motor number configuration
            "P2372": parse_dds_float,  # Motor staging cycling
            "P2371": parse_dds_float,  # Motor staging cycling
            "P2372": parse_dds_float,  # Motor staging cycling
            "P2378": parse_dds_float,  # Motor staging frequency [%]
            "P2371": parse_dds_float,  # Motor staging cycling
            "r4000": parse_r4000_mpc_status,
        }

    # -------------------------------------------------------------------------
    # Low-level WebSocket
    # -------------------------------------------------------------------------

    def connect(self, timeout: float = 5.0):
        """Open TCP + WebSocket handshake (without verifying Sec-WebSocket-Accept).

        Args:
            timeout: Socket connection timeout in seconds.
        """
        key = base64.b64encode(os.urandom(16)).decode("ascii")

        request_lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "Origin: http://192.168.1.1",
            "",
            "",
        ]
        request = "\r\n".join(request_lines).encode("ascii")

        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.sendall(request)

        # Read HTTP headers until \r\n\r\n
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed during handshake")
            response += chunk

        try:
            header = response.split(b"\r\n\r\n", 1)[0].decode("ascii", errors="replace")
        except Exception:
            header = "<failed to decode header>"
        logger.debug("Handshake response header:\n%s", header)
        logger.info("WebSocket connected (Sinamics V20 Smart Access)")

    def close(self):
        """Close underlying socket."""
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _ensure_sock(self):
        if not self.sock:
            raise RuntimeError("Socket is not connected. Call connect() first.")

    def _send_frame(self, text: str):
        """Send a WebSocket text frame.

        Note:
            Many embedded devices prefer line endings; append newline.
        """
        self._ensure_sock()
        payload = (text + "\n").encode("utf-8")

        b1 = 0x80 | 0x1  # FIN + text opcode
        mask_bit = 0x80
        length = len(payload)

        header = bytearray([b1])

        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))

        mask = os.urandom(4)
        header.extend(mask)

        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        self.sock.sendall(header + masked_payload)

    def _recv_frame(self) -> Optional[str]:
        """Receive one WebSocket text frame.

        Returns:
            str: Decoded payload.
            None: If server closed the connection.
        """
        self._ensure_sock()

        first_two = self.sock.recv(2)
        if not first_two:
            logger.warning("Server closed the connection (no frame header)")
            return None
        if len(first_two) < 2:
            logger.warning("Connection closed while reading frame header")
            return None

        b1, b2 = first_two
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F

        if opcode == 0x8:  # close frame
            logger.info("Received WebSocket close frame")
            return None

        if length == 126:
            ext = self.sock.recv(2)
            if len(ext) < 2:
                logger.warning("Connection closed while reading extended length (16-bit)")
                return None
            (length,) = struct.unpack("!H", ext)
        elif length == 127:
            ext = self.sock.recv(8)
            if len(ext) < 8:
                logger.warning("Connection closed while reading extended length (64-bit)")
                return None
            (length,) = struct.unpack("!Q", ext)

        mask = b""
        if masked:
            mask = self.sock.recv(4)
            if len(mask) < 4:
                logger.warning("Connection closed while reading mask")
                return None

        payload = b""
        while len(payload) < length:
            chunk = self.sock.recv(length - len(payload))
            if not chunk:
                logger.warning("Connection closed mid-frame")
                return None
            payload += chunk

        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        return payload.decode("utf-8", errors="replace").strip()

    def _recv_one_matching(self, prefix: str, max_skip: int = 50) -> Optional[str]:
        """Read frames until one starts with *prefix*; silently skip others."""
        skipped = 0
        while skipped < max_skip:
            frame = self._recv_frame()
            if frame is None:
                return None
            if frame.startswith(prefix):
                return frame
            logger.debug("Skip unsolicited frame while waiting for %s: %s", prefix, frame)
            skipped += 1
        logger.warning("Max skipped frames exceeded while waiting for %s", prefix)
        return None

    def _recv_many_matching(
        self, prefix: str, expected: int, max_skip_per_reply: int = 50
    ) -> List[str]:
        """Collect *expected* frames starting with *prefix*."""
        replies: List[str] = []
        while len(replies) < expected:
            frame = self._recv_one_matching(prefix, max_skip=max_skip_per_reply)
            if frame is None:
                break
            replies.append(frame)
        return replies

    # -------------------------------------------------------------------------
    # Generic protocol helpers
    # -------------------------------------------------------------------------

    def send_command(self, cmd: str, expect_prefix: str) -> Optional[str]:
        """Send *cmd* and return the first frame that starts with *expect_prefix*."""
        self._send_frame(cmd)
        logger.debug(">>> %s", cmd)
        resp = self._recv_one_matching(expect_prefix)
        if resp:
            logger.debug("<<< %s", resp)
        return resp

    def send_batch(self, cmds: List[str], expect_prefix: str) -> List[str]:
        """Send a batch and return *only* frames that start with *expect_prefix*."""
        if not cmds:
            return []
        payload = "||".join(cmds)
        self._send_frame(payload)
        logger.debug(">>> %s", payload)

        replies = self._recv_many_matching(expect_prefix, expected=len(cmds))
        for r in replies:
            logger.debug("<<< %s", r)
        return replies

    # -------------------------------------------------------------------------
    # High-level operations
    # -------------------------------------------------------------------------

    def query_ident(self) -> Optional[Dict[str, Any]]:
        """queryIdent -> queryIdent,200,<string with model & params>"""
        resp = self.send_command("queryIdent", expect_prefix="queryIdent,")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 2 or parts[0] != "queryIdent":
            logger.warning("Unexpected queryIdent response: %s", resp)
            return None

        try:
            status = int(parts[1])
        except ValueError:
            logger.warning("Invalid status in queryIdent: %s", parts[1])
            return None

        info = parts[2] if len(parts) > 2 else ""
        info_parts = info.split("&")
        return {
            "status": status,
            "raw": info,
            "model": info_parts[0] if len(info_parts) > 0 else None,
            "extra": info_parts[1:],  # Version, voltage, frequency, etc.
        }

    def report_status(self) -> Optional[Dict[str, Any]]:
        """reportStatus -> reportStatus,200,0,en00000000013338,4"""
        resp = self.send_command("reportStatus", expect_prefix="reportStatus,")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 3 or parts[0] != "reportStatus":
            logger.warning("Unexpected reportStatus response: %s", resp)
            return None

        try:
            status = int(parts[1])
        except ValueError:
            logger.warning("Invalid status in reportStatus: %s", parts[1])
            return None

        return {
            "status": status,
            "error_code": parts[2],
            "raw_fields": parts[3:],
        }

    def fa_sum(self) -> Optional[Dict[str, Any]]:
        """faSum -> faSum,200,0,0,4 (aggregated faults/warnings)."""
        resp = self.send_command("faSum", expect_prefix="faSum,")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 2 or parts[0] != "faSum":
            logger.warning("Unexpected faSum response: %s", resp)
            return None

        try:
            status = int(parts[1])
        except ValueError:
            logger.warning("Invalid status in faSum: %s", parts[1])
            return None

        nums: List[int] = []
        for x in parts[2:]:
            try:
                nums.append(int(x))
            except ValueError:
                # Skip non-numeric fields but do not fail
                pass

        return {
            "status": status,
            "values": nums,
            "raw_fields": parts[2:],
        }

    def read_param(self, name: str, index: int = -1, length: int = 4) -> Optional[Dict[str, Any]]:
        """Read a single parameter via readPara command.

        Protocol:
            readPara,11,<name>,<index>,<length> -> readPara,200,<name>,<index>,<value>

        Args:
            name: Parameter name, e.g. 'P0007', 'P0003', 'r0002'.
            index: Parameter index, default -1.
            length: Data length, default 4.

        Returns:
            Dict with status, name, index and raw value when successful, otherwise None.
        """
        cmd = f"readPara,11,{name},{index},{length}"
        resp = self.send_command(cmd, expect_prefix="readPara,")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 5 or parts[0] != "readPara":
            logger.warning("Unexpected readPara response: %s", resp)
            return None

        try:
            status = int(parts[1])
            idx = int(parts[3])
        except ValueError:
            logger.warning("Invalid status/index in readPara: %s", parts)
            return None

        return {
            "status": status,
            "name": parts[2],
            "index": idx,
            "value_raw": parts[4],
        }

    def read_params_batch(self, names: List[str], index: int = -1, length: int = 4) -> Dict[str, Dict[str, Any]]:
        """Read multiple parameters using a single batch (||-joined) request.

        Args:
            names: List of parameter names.
            index: Parameter index, default -1.
            length: Data length, default 4.

        Returns:
            Dict name -> info dict with status, index, and raw value.
        """
        if not names:
            return {}

        cmds = [f"readPara,11,{n},{index},{length}" for n in names]
        replies = self.send_batch(cmds, expect_prefix="readPara,")
        results: Dict[str, Dict[str, Any]] = {}
        for resp in replies:
            parts = resp.split(",")
            if len(parts) < 5 or parts[0] != "readPara":
                logger.warning("Unexpected readPara batch response: %s", resp)
                continue

            try:
                status = int(parts[1])
                idx = int(parts[3])
            except ValueError:
                logger.warning("Invalid status/index in batch readPara: %s", parts)
                continue
            name = parts[2]
            results[name] = {
                "status": status,
                "index": idx,
                "value_raw": parts[4],
            }
        return results
        
    def write_param(self, name: str, value: Any, index: int = -1) -> Optional[Dict[str, Any]]:
        """Write a single parameter via the ``writePara`` command.

        Sends a writePara request and parses the response.  The command format
        was inferred from the existing readPara implementation; adjust if Siemens publishes an official spec.

        Args:
            name: Parameter name, e.g. 'P0010'.
            value: Value to write; numeric types will be converted to string.
            index: Parameter index, default -1.

        Returns:
            Dict with status, name, index and result code, or None if no response.
        """
        val_str = str(value)
        cmd = f"writePara,11,{name},{index},{val_str}"
        resp = self.send_command(cmd, expect_prefix="writePara,")
        if resp is None:
            return None
        parts = resp.split(",")
        if len(parts) < 4 or parts[0] != "writePara":
            logger.warning("Unexpected writePara response: %s", resp)
            return None
        try:
            status = int(parts[1])
            idx = int(parts[3])
        except ValueError:
            logger.warning("Invalid status/index in writePara: %s", parts)
            return None
        result = parts[4] if len(parts) > 4 else None
        return {
            "status": status,
            "name": parts[2],
            "index": idx,
            "result": result,
        }

    # -------------------------------------------------------------------------
    # Monitoring loop
    # -------------------------------------------------------------------------

    def monitor(
        self,
        interval_sec: float,
        params: List[str],
        callback: Optional[Callable[[Dict[str, Dict[str, Any]]], None]] = None,
    ):
        """Simple monitoring loop: reads given params by batch and calls callback.

        Args:
            interval_sec: Interval in seconds between reads.
            params: Parameter names to read.
            callback: Optional callback taking the result dict.
        """
        try:
            while True:
                try:
                    data = self.read_params_batch(params)
                except (OSError, RuntimeError) as exc:
                    logger.error("Read error: %s", exc)
                    break

                if callback:
                    callback(data)
                else:
                    logger.info("Monitor data: %s", pformat(data))
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user.")

    def read_station_state(self) -> Dict[str, Any]:
        """Read key parameters and return an aggregated station state."""
        # Parameters to read in one batch
        param_names = [
            "r0052",   # Active status word 1
            "r4000",   # Multi-pump control status word
            "r0020",   # Frequency setpoint before RFG [Hz]
            "r0021",   # Actual filtered frequency [Hz]
            "r2260",   # PID setpoint after PID-RFG
            "r2294",   # Act. PID output
            "r2273",   # PID error
            "P1080",   # Min frequency [Hz]
            "P1082",   # Max frequency [Hz]
            "P2390",   # PID hibernation setpoint [%]
            "r0072",   # Actual output voltage [V]
            "r4026",   # Operating hours motor 1 [h]
            "r4027",   # Operating hours motor 2 [h]
            "P2378",   # Motor staging frequency [%]

        ]

        raw_params = self.read_params_batch(param_names)

        # Retrieve faSum/reportStatus separately
        rep = self.report_status()

        def safe_parse(name: str, raw: Optional[str]) -> Any:
            if raw is None:
                return None
            parser = self.parsers.get(name)
            if not parser:
                return raw
            try:
                return parser(raw)
            except Exception as exc:
                logger.warning("Parse error for %s with raw=%s: %s", name, raw, exc)
                return {"raw": raw, "parse_error": str(exc)}

        # Parse "heavy" status words
        r0052_raw = raw_params.get("r0052", {}).get("value_raw")
        r4000_raw = raw_params.get("r4000", {}).get("value_raw")

        drive_status = safe_parse("r0052", r0052_raw) or {}
        mpc_status = safe_parse("r4000", r4000_raw) or {}

        # Frequencies / voltage
        freq_set_before = safe_parse("r0020", raw_params.get("r0020", {}).get("value_raw"))
        freq_actual = safe_parse("r0021", raw_params.get("r0021", {}).get("value_raw"))
        u_out = safe_parse("r0072", raw_params.get("r0072", {}).get("value_raw"))

        # PID
        pid_set_after = safe_parse("r2260", raw_params.get("r2260", {}).get("value_raw"))
        pid_out = safe_parse("r2294", raw_params.get("r2294", {}).get("value_raw"))
        pid_err = safe_parse("r2273", raw_params.get("r2273", {}).get("value_raw"))
        pid_hib = safe_parse("P2390", raw_params.get("P2390", {}).get("value_raw"))

        # Frequency limits
        f_min = safe_parse("P1080", raw_params.get("P1080", {}).get("value_raw"))
        f_max = safe_parse("P1082", raw_params.get("P1082", {}).get("value_raw"))
                staging_pct = safe_parse("P2378", raw_params.get("P2378", {}).get("value_raw"))
        # Derived values from percentages
        hib_hz = pid_hib * f_max / 100 if (isinstance(pid_hib, (int, float)) and isinstance(f_max, (int, float))) else None
        staging_hz = staging_pct * f_max / 100 if (isinstance(staging_pct, (int, float)) and isinstance(f_max, (int, float))) else None


        # Motor operating hours
        h_m1 = safe_parse("r4026", raw_params.get("r4026", {}).get("value_raw"))
        h_m2 = safe_parse("r4027", raw_params.get("r4027", {}).get("value_raw"))

        # High-level status
        any_motor_running = any([
            mpc_status.get("motor1_on"),
            mpc_status.get("motor2_on"),
            mpc_status.get("motor3_on"),
            mpc_status.get("motor4_on"),
        ])

        has_fault = any([
            drive_status.get("converter_fault_active"),
            drive_status.get("converter_overload"),
            drive_status.get("motor_overload"),
        ])

        has_warning = any([
            drive_status.get("converter_warning_active"),
            drive_status.get("current_torque_limit_warning"),
        ])

        if has_fault:
            high_level_state = "fault"
        elif drive_status.get("operation_enabled") and any_motor_running:
            high_level_state = "running"
        elif drive_status.get("converter_ready"):
            high_level_state = "ready"
        else:
            high_level_state = "stopped"

        return {
            "timestamp": time.time(),
            "high_level": {
                "state": high_level_state,  # "running" / "stopped" / "ready" / "fault"
                "has_fault": has_fault,
                "has_warning": has_warning,
            },
            "drive": {
                "status_word": drive_status,
                "report_status": rep,
            },
            "multi_pump": {
                "status": mpc_status,
                "running_motors": [
                    i for i, flag in enumerate(
                        [
                            mpc_status.get("motor1_on"),
                            mpc_status.get("motor2_on"),
                            mpc_status.get("motor3_on"),
                            mpc_status.get("motor4_on"),
                        ], start=1,) if flag]
                "staging_frequency_pct": staging_pct,
                "staging_frequency_hz": staging_hz,
            },        
            "frequency": {
                "setpoint_before_rfg_hz": freq_set_before,
                "actual_filtered_hz": freq_actual,
                "min_hz": f_min,
                "max_hz": f_max,
            },
            "voltage": {"u_out_v": u_out},
            "pid": {
                "setpoint_after_rfg": pid_set_after,
                "output": pid_out,
                "error": pid_err,
                "hibernation_setpoint_pct": pid_hib,
                "hibernation_setpoint_hz": hib_hz,
            },
            "operating_hours": {
                "motor1_h": h_m1,
                "motor2_h": h_m2,
            },
            "raw_params": raw_params,
        }


def parse_r0052(status_word) -> dict:
    """Parse r0052 (CO/BO: Active status word 1, U16) into flags."""
    status_word = int(status_word)

    def bit(n: int) -> int:
        return (status_word >> n) & 0x1

    # Active-high bits (1 = active state)
    converter_ready = bool(bit(0))
    ready_to_run = bool(bit(1))
    operation_enabled = bool(bit(2))
    converter_fault_active = bool(bit(3))
    on_inhibit_active = bool(bit(6))
    converter_warning_active = bool(bit(7))
    pzd_control = bool(bit(9))
    freq_ge_fmax = bool(bit(10))
    brake_open = bool(bit(12))
    motor_runs_right = bool(bit(14))

    # Active-low bits (0 = active state)
    off2_active = (bit(4) == 0)
    off3_active = (bit(5) == 0)
    current_torque_limit_warn = (bit(11) == 0)
    motor_overload = (bit(13) == 0)
    converter_overload = (bit(15) == 0)

    # Deviation setpoint/act.value: 1 = No, 0 = Yes → active when bit = 0
    deviation_active = (bit(8) == 0)

    return {
        "raw": status_word,
        "bits": {i: bit(i) for i in range(16)},

        "converter_ready": converter_ready,
        "ready_to_run": ready_to_run,
        "operation_enabled": operation_enabled,
        "converter_fault_active": converter_fault_active,

        "off2_active": off2_active,
        "off3_active": off3_active,
        "on_inhibit_active": on_inhibit_active,

        "converter_warning_active": converter_warning_active,
        "deviation_active": deviation_active,
        "pzd_control": pzd_control,
        "freq_ge_fmax": freq_ge_fmax,

        "current_torque_limit_warning": current_torque_limit_warn,
        "brake_open": brake_open,
        "motor_overload": motor_overload,
        "motor_runs_right": motor_runs_right,
        "converter_overload": converter_overload,
    }


def parse_dds_float(value_raw: str) -> float:
    """
    Decode Siemens DDS Float 1/2/3/4 from the high 16 bits.
    Багато параметрів типу 'Float' у V20 через SmartAccess приходять
    як старше слово IEEE754 float.
    """
    hi = int(value_raw)          # e.g. 16968
    u32 = hi << 16               # 0x4248 -> 0x42480000
    return struct.unpack("<f", struct.pack("<I", u32))[0]


def parse_r4000_mpc_status(status_word) -> dict:
    """
    Парсер r4000 – Multi-pump control status word (U16).
    """
    status_word = int(status_word)

    def bit(n: int) -> int:
        return (status_word >> n) & 0x1

    on_off1 = bool(bit(0))
    m1_on = bool(bit(1))
    m2_on = bool(bit(2))
    m3_on = bool(bit(3))
    m4_on = bool(bit(4))
    switching_in_progress = bool(bit(5))
    no_idle_motor_running = bool(bit(6))
    off2_active = bool(bit(7))

    return {
        "raw": status_word,
        "bits": {i: bit(i) for i in range(16)},

        "on_off1": on_off1,
        "motor1_on": m1_on,
        "motor2_on": m2_on,
        "motor3_on": m3_on,
        "motor4_on": m4_on,
        "switching_in_progress": switching_in_progress,
        "no_idle_motor_running": no_idle_motor_running,
        "off2_active": off2_active,
    }


# if __name__ == "__main__":
#     client = SinamicsV20Client("192.168.1.1", 80, "/")
#
#     client.connect()
#
#     ident = client.query_ident()
#     print("IDENT:")
#     pprint(ident)
#
#     status = client.report_status()
#     print("STATUS:")
#     pprint(status)
#
#     fa = client.fa_sum()
#     print("FA:")
#     pprint(fa)
#
#
#     state = client.read_station_state()
#     pprint(state, indent=2)
#
#     client.close()
