import socket
import base64
import hashlib
import os
import struct
import time
import logging
from pprint import pformat
from typing import Dict, List, Optional, Callable, Any

logger = logging.getLogger(__name__)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class SinamicsV20Client:
    """Minimal WebSocket client for Sinamics V20 Smart Access."""

    def __init__(self, host: str = "192.168.1.1", port: int = 80, path: str = "/"):
        self.host = host
        self.port = port
        self.path = path
        self.sock: Optional[socket.socket] = None
        self._recv_buffer = bytearray()
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
            "P2378": parse_dds_float,  # Motor staging frequency [%]
            "r4000": parse_r4000_mpc_status,
        }

    # -------------------------------------------------------------------------
    # Low-level WebSocket
    # -------------------------------------------------------------------------

    def connect(
        self,
        timeout: float = 5.0,
        read_timeout: Optional[float] = None,
        handshake_retries: int = 2,
        handshake_retry_delay: float = 0.5,
    ):
        """Open TCP + WebSocket handshake (without verifying Sec-WebSocket-Accept).

        Args:
            timeout: Socket connection timeout in seconds.
        """
        last_exc: Optional[Exception] = None
        attempts = max(1, int(handshake_retries))

        for attempt in range(1, attempts + 1):
            key_bytes = os.urandom(16)
            key = base64.b64encode(key_bytes).decode("ascii")
            expected_accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            origin = f"http://{self.host}"

            request_lines = [
                f"GET {self.path} HTTP/1.1",
                f"Host: {self.host}:{self.port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                f"Origin: {origin}",
                "",
                "",
            ]
            request = "\r\n".join(request_lines).encode("ascii")

            try:
                self.close()
                self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
                self.sock.settimeout(timeout)
                self.sock.sendall(request)
                self._recv_buffer.clear()

                # Read HTTP headers until \r\n\r\n
                response = b""
                while b"\r\n\r\n" not in response:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise RuntimeError("Connection closed during handshake")
                    response += chunk
                    if len(response) > 65536:
                        raise RuntimeError("Handshake header too large")

                try:
                    header_bytes, remainder = response.split(b"\r\n\r\n", 1)
                    header = header_bytes.decode("ascii", errors="replace")
                except Exception:
                    header = "<failed to decode header>"
                    remainder = b""
                logger.debug("Handshake response header:\n%s", header)

                status_line = ""
                headers = {}
                if header != "<failed to decode header>":
                    lines = header.split("\r\n")
                    if lines:
                        status_line = lines[0]
                    for line in lines[1:]:
                        if ":" not in line:
                            continue
                        key_name, value = line.split(":", 1)
                        headers[key_name.strip().lower()] = value.strip()

                if " 101 " not in f" {status_line} " and not status_line.endswith(" 101"):
                    raise RuntimeError(
                        f"Invalid WebSocket handshake status: {status_line or '<missing>'}"
                    )
                if headers.get("upgrade", "").lower() != "websocket":
                    raise RuntimeError(
                        f"Invalid WebSocket Upgrade header: {headers.get('upgrade')!r}"
                    )
                if "upgrade" not in headers.get("connection", "").lower():
                    raise RuntimeError(
                        f"Invalid WebSocket Connection header: {headers.get('connection')!r}"
                    )
                accept_header = headers.get("sec-websocket-accept", "").strip()
                if accept_header != expected_accept:
                    logger.debug(
                        "Handshake accept mismatch (attempt %d/%d): expected=%r got=%r",
                        attempt,
                        attempts,
                        expected_accept,
                        accept_header,
                    )
                    raise RuntimeError("Invalid Sec-WebSocket-Accept in handshake response")

                self._recv_buffer.extend(remainder)
                effective_read_timeout = timeout if read_timeout is None else read_timeout
                self.sock.settimeout(effective_read_timeout)
                logger.info("WebSocket connected (Sinamics V20 Smart Access)")
                return

            except Exception as exc:
                last_exc = exc
                self.close()
                is_retryable = isinstance(exc, (TimeoutError, OSError, socket.error)) or (
                    isinstance(exc, RuntimeError) and "handshake" in str(exc).lower()
                )
                if attempt >= attempts or not is_retryable:
                    raise
                logger.warning(
                    "WebSocket connect attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    handshake_retry_delay,
                )
                time.sleep(max(0.0, handshake_retry_delay))

        if last_exc:
            raise last_exc

    def close(self):
        """Close underlying socket."""
        self._recv_buffer.clear()
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _ensure_sock(self):
        if not self.sock:
            raise RuntimeError("Socket is not connected. Call connect() first.")

    def _recv_exact(self, nbytes: int) -> Optional[bytes]:
        """Read exactly ``nbytes`` from the socket (using the internal buffer)."""
        self._ensure_sock()
        while len(self._recv_buffer) < nbytes:
            chunk = self.sock.recv(max(4096, nbytes - len(self._recv_buffer)))
            if not chunk:
                return None
            self._recv_buffer.extend(chunk)
        out = bytes(self._recv_buffer[:nbytes])
        del self._recv_buffer[:nbytes]
        return out

    def _send_raw_frame(self, opcode: int, payload: bytes):
        """Send a client WebSocket frame with masking."""
        self._ensure_sock()

        b1 = 0x80 | (opcode & 0x0F)  # FIN + opcode
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

    def _send_frame(self, text: str):
        """Send a WebSocket text frame.

        Note:
            Many embedded devices prefer line endings; append newline.
        """
        payload = (text + "\n").encode("utf-8")
        self._send_raw_frame(0x1, payload)

    def _send_pong(self, payload: bytes = b""):
        """Reply to a WebSocket ping."""
        try:
            self._send_raw_frame(0xA, payload)
        except Exception as exc:
            logger.debug("Failed to send pong frame: %s", exc)

    def _recv_frame(self) -> Optional[str]:
        """Receive one WebSocket text frame.

        Returns:
            str: Decoded payload.
            None: If server closed the connection.
        """
        self._ensure_sock()

        while True:
            first_two = self._recv_exact(2)
            if not first_two:
                logger.warning("Server closed the connection (no frame header)")
                return None
            if len(first_two) < 2:
                logger.warning("Connection closed while reading frame header")
                return None

            b1, b2 = first_two
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F

            if length == 126:
                ext = self._recv_exact(2)
                if not ext or len(ext) < 2:
                    logger.warning("Connection closed while reading extended length (16-bit)")
                    return None
                (length,) = struct.unpack("!H", ext)
            elif length == 127:
                ext = self._recv_exact(8)
                if not ext or len(ext) < 8:
                    logger.warning("Connection closed while reading extended length (64-bit)")
                    return None
                (length,) = struct.unpack("!Q", ext)

            mask = b""
            if masked:
                mask = self._recv_exact(4)
                if not mask or len(mask) < 4:
                    logger.warning("Connection closed while reading mask")
                    return None

            payload = self._recv_exact(length) if length else b""
            if payload is None:
                logger.warning("Connection closed mid-frame")
                return None

            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close frame
                logger.info("Received WebSocket close frame")
                return None
            if opcode == 0x9:  # ping
                logger.debug("Received WebSocket ping; replying with pong")
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                logger.debug("Received WebSocket pong")
                continue
            if opcode != 0x1:
                logger.debug("Ignoring unsupported WebSocket opcode=0x%x (fin=%s)", opcode, fin)
                continue
            if not fin:
                logger.warning("Received fragmented text frame; continuing with partial payload")

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
        if len(replies) != len(cmds):
            logger.warning(
                "Batch response count mismatch for %s: expected=%d got=%d",
                expect_prefix,
                len(cmds),
                len(replies),
            )
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

    def read_params_batch(
        self,
        names: List[str],
        index: int = -1,
        length: int = 4,
        strict_count: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
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
        if strict_count and len(replies) != len(names):
            raise RuntimeError(
                f"Incomplete batch response: expected {len(names)} replies, got {len(replies)}"
            )
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
        if strict_count:
            missing = [name for name in names if name not in results]
            if missing:
                raise RuntimeError(
                    f"Incomplete batch response: missing values for {', '.join(missing)}"
                )
        return results

    def read_params_chunked(
        self,
        names: List[str],
        index: int = -1,
        length: int = 4,
        batch_size: Optional[int] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Read parameters in smaller batches to reduce load on the device."""
        if not names:
            return {}
        if not batch_size or batch_size <= 0 or batch_size >= len(names):
            return self.read_params_batch(names, index=index, length=length, strict_count=True)

        results: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(names), batch_size):
            chunk = names[start : start + batch_size]
            chunk_results = self.read_params_batch(
                chunk,
                index=index,
                length=length,
                strict_count=True,
            )
            results.update(chunk_results)
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

    def read_station_state(self, batch_size: Optional[int] = None) -> Dict[str, Any]:
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

        raw_params = self.read_params_chunked(param_names, batch_size=batch_size)

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
                "running_motors": [i for i, flag in enumerate([
                    mpc_status.get("motor1_on"),
                    mpc_status.get("motor2_on"),
                    mpc_status.get("motor3_on"),
                    mpc_status.get("motor4_on"),], start=1,) if flag],
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
