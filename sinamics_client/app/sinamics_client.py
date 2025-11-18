import socket
import base64
import os
import struct
import time
from pprint import pprint
from typing import Dict, List, Optional, Callable, Any


class SinamicsV20Client:
    def __init__(self, host: str = "192.168.1.1", port: int = 80, path: str = "/"):
        self.host = host
        self.port = port
        self.path = path
        self.sock: Optional[socket.socket] = None
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
            "r2294": parse_dds_float,  # Act.PID output
            "P2390": parse_dds_float,  # PID hibernation setpoint [%]
            "r4026": parse_dds_float,  # Multi-pump abs. operating hours: motor 1 [h]
            "r4027": parse_dds_float,  # Multi-pump abs. operating hours: motor 2 [h]
            "r2273": parse_dds_float,  # PID error
            "P4013": parse_dds_float,  # Multi-pump control motor number configuration (залежить від типу в мануалі)
            "P2372": parse_dds_float,  # Motor staging cycling
            "P2371": parse_dds_float,  # Motor staging cycling
            "r4000": parse_r4000_mpc_status,
    }

    # -------------------------------------------------------------------------
    # Low-level WebSocket
    # -------------------------------------------------------------------------

    def connect(self, timeout: float = 5.0):
        """
        Open TCP + WebSocket handshake (без перевірки Sec-WebSocket-Accept).
        timeout – щоб не висіти вічно, якщо девайс не відповідає.
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

        # Read HTTP headers до \r\n\r\n
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed during handshake")
            response += chunk

        print("=== Handshake response ===")
        print(response.split(b"\r\n\r\n", 1)[0].decode("ascii", errors="replace"))
        print("==========================")
        print("WebSocket connected (Sinamics V20 Smart Access)")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _ensure_sock(self):
        if not self.sock:
            raise RuntimeError("Socket is not connected. Call connect() first.")

    def _send_frame(self, text: str):
        """
        Send WebSocket text frame.
        Багато embedded-пристроїв люблять закінчення рядка, тому додаємо \n.
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
        """
        Receive one WebSocket text frame.
        Returns:
            str  - payload
            None - if server closed the connection.
        """
        self._ensure_sock()

        first_two = self.sock.recv(2)
        if not first_two:
            print("Server closed the connection (no frame header)")
            return None
        if len(first_two) < 2:
            print("Connection closed while reading frame header")
            return None

        b1, b2 = first_two
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F

        if opcode == 0x8:  # close
            print("Received close frame")
            return None

        if length == 126:
            ext = self.sock.recv(2)
            if len(ext) < 2:
                print("Connection closed while reading extended length")
                return None
            (length,) = struct.unpack("!H", ext)
        elif length == 127:
            ext = self.sock.recv(8)
            if len(ext) < 8:
                print("Connection closed while reading extended length")
                return None
            (length,) = struct.unpack("!Q", ext)

        mask = b""
        if masked:
            mask = self.sock.recv(4)
            if len(mask) < 4:
                print("Connection closed while reading mask")
                return None

        payload = b""
        while len(payload) < length:
            chunk = self.sock.recv(length - len(payload))
            if not chunk:
                print("Connection closed in the middle of a frame")
                return None
            payload += chunk

        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        return payload.decode("utf-8", errors="replace").strip()

    # -------------------------------------------------------------------------
    # Generic protocol helpers
    # -------------------------------------------------------------------------

    def send_command(self, cmd: str) -> Optional[str]:
        """Send single command (faSum, readPara, queryIdent, ...) and read one reply."""
        self._send_frame(cmd)
        print(f">>> {cmd}")
        resp = self._recv_frame()
        if resp is not None:
            print(f"<<< {resp}")
        return resp

    def send_batch(self, cmds: List[str]) -> List[str]:
        """
        Send multiple commands в одному фреймі через '||'.
        Returns list of replies (по одному фрейму на команду).
        """
        if not cmds:
            return []

        payload = "||".join(cmds)
        self._send_frame(payload)
        print(f">>> {payload}")

        replies = []
        for _ in cmds:
            resp = self._recv_frame()
            if resp is None:
                break
            print(f"<<< {resp}")
            replies.append(resp)
        return replies

    # -------------------------------------------------------------------------
    # High-level operations
    # -------------------------------------------------------------------------

    def query_ident(self) -> Optional[Dict[str, Any]]:
        """
        queryIdent -> queryIdent,200,<string with model & params>
        """
        resp = self.send_command("queryIdent")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 2 or parts[0] != "queryIdent":
            print("Unexpected queryIdent response:", resp)
            return None

        try:
            status = int(parts[1])
        except ValueError:
            print("Invalid status in queryIdent:", parts[1])
            return None

        info = parts[2] if len(parts) > 2 else ""
        info_parts = info.split("&")
        return {
            "status": status,
            "raw": info,
            "model": info_parts[0] if len(info_parts) > 0 else None,
            "extra": info_parts[1:],  # версію, напругу, частоту і т.д. можна розібрати з мануалу
        }

    def report_status(self) -> Optional[Dict[str, Any]]:
        """
        reportStatus -> reportStatus,200,0,en00000000013338,4
        """
        resp = self.send_command("reportStatus")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 3 or parts[0] != "reportStatus":
            print("Unexpected reportStatus response:", resp)
            return None

        try:
            status = int(parts[1])
        except ValueError:
            print("Invalid status in reportStatus:", parts[1])
            return None

        return {
            "status": status,
            "error_code": parts[2],
            "raw_fields": parts[3:],
        }

    def fa_sum(self) -> Optional[Dict[str, Any]]:
        """
        faSum -> faSum,200,0,0,4
        згідно мануалу це сумарний статус аварій/попереджень (потрібно звірити значення).
        """
        resp = self.send_command("faSum")
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 2 or parts[0] != "faSum":
            print("Unexpected faSum response:", resp)
            return None

        try:
            status = int(parts[1])
        except ValueError:
            print("Invalid status in faSum:", parts[1])
            return None

        nums: List[int] = []
        for x in parts[2:]:
            try:
                nums.append(int(x))
            except ValueError:
                # пропускаємо не-числові поля, але не падаємо
                pass

        return {
            "status": status,
            "values": nums,
            "raw_fields": parts[2:],
        }

    def read_param(self, name: str, index: int = -1, length: int = 4) -> Optional[Dict[str, Any]]:
        """
        readPara,11,<name>,<index>,<length>
        -> readPara,200,<name>,<index>,<value>
        name: 'P0007', 'P0003', 'r0002', ...
        """
        cmd = f"readPara,11,{name},{index},{length}"
        resp = self.send_command(cmd)
        if resp is None:
            return None

        parts = resp.split(",")
        if len(parts) < 5 or parts[0] != "readPara":
            print("Unexpected readPara response:", resp)
            return None

        try:
            status = int(parts[1])
            idx = int(parts[3])
        except ValueError:
            print("Invalid status/index in readPara:", parts)
            return None

        return {
            "status": status,
            "name": parts[2],
            "index": idx,
            "value_raw": parts[4],
        }

    def read_params_batch(self, names: List[str], index: int = -1, length: int = 4) -> Dict[str, Dict[str, Any]]:
        """
        Відправляє кілька readPara в одному фреймі через ||
        і повертає dict name -> info.
        """
        if not names:
            return {}

        cmds = [f"readPara,11,{n},{index},{length}" for n in names]
        replies = self.send_batch(cmds)

        results: Dict[str, Dict[str, Any]] = {}
        for resp in replies:
            parts = resp.split(",")
            if len(parts) < 5 or parts[0] != "readPara":
                print("Unexpected readPara batch response:", resp)
                continue

            try:
                status = int(parts[1])
                idx = int(parts[3])
            except ValueError:
                print("Invalid status/index in batch readPara:", parts)
                continue

            name = parts[2]
            results[name] = {
                "status": status,
                "index": idx,
                "value_raw": parts[4],
            }
        return results

    # -------------------------------------------------------------------------
    # Monitoring loop
    # -------------------------------------------------------------------------

    def monitor(
        self,
        interval_sec: float,
        params: List[str],
        callback: Optional[Callable[[Dict[str, Dict[str, Any]]], None]] = None,
    ):
        """
        Простий цикл моніторингу:
        - кожні interval_sec читає задані параметри (batch read)
        - викликає callback(result_dict) або просто друкує їх.
        """
        try:
            while True:
                try:
                    data = self.read_params_batch(params)
                except (OSError, RuntimeError) as e:
                    print(f"Read error: {e}")
                    break

                if callback:
                    callback(data)
                else:
                    print("MON:", data)
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            print("Monitoring stopped by user.")
    def read_station_state(self) -> Dict[str, Any]:
        """
        Прочитати ключові параметри і повернути зведений стан насосної станції
        (multi-pump + привід + PID + частоти).
        """

        # Які параметри читаємо одним batch
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
        ]

        raw_params = self.read_params_batch(param_names)

        # Підтягуємо faSum/reportStatus окремо
        fa = self.fa_sum()
        rep = self.report_status()

        # Локально підтягнемо парсери (можеш винести у глобальну змінну)
        from pprint import pprint  # якщо хочеш дебажити

        def safe_parse(name: str, raw: Optional[str]) -> Any:
            if raw is None:
                return None
            parser = self.parsers.get(name)
            if not parser:
                return raw
            try:
                return parser(raw)
            except Exception as e:
                return {"raw": raw, "parse_error": str(e)}

        # Розпарсимо “важкі” статуси
        r0052_raw = raw_params.get("r0052", {}).get("value_raw")
        r4000_raw = raw_params.get("r4000", {}).get("value_raw")

        drive_status = safe_parse("r0052", r0052_raw) or {}
        mpc_status = safe_parse("r4000", r4000_raw) or {}

        # Частоти / напруга
        freq_set_before = safe_parse("r0020", raw_params.get("r0020", {}).get("value_raw"))
        freq_actual = safe_parse("r0021", raw_params.get("r0021", {}).get("value_raw"))
        u_out = safe_parse("r0072", raw_params.get("r0072", {}).get("value_raw"))

        # PID
        pid_set_after = safe_parse("r2260", raw_params.get("r2260", {}).get("value_raw"))
        pid_out = safe_parse("r2294", raw_params.get("r2294", {}).get("value_raw"))
        pid_err = safe_parse("r2273", raw_params.get("r2273", {}).get("value_raw"))
        pid_hib = safe_parse("P2390", raw_params.get("P2390", {}).get("value_raw"))

        # Межі частоти
        f_min = safe_parse("P1080", raw_params.get("P1080", {}).get("value_raw"))
        f_max = safe_parse("P1082", raw_params.get("P1082", {}).get("value_raw"))

        # Напрацювання двигунів
        h_m1 = safe_parse("r4026", raw_params.get("r4026", {}).get("value_raw"))
        h_m2 = safe_parse("r4027", raw_params.get("r4027", {}).get("value_raw"))

        # Умовні статуси “зручною мовою”
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
                "state": high_level_state,       # "running" / "stopped" / "ready" / "fault"
                "has_fault": has_fault,
                "has_warning": has_warning,
            },

            "drive": {
                "status_word": drive_status,
                "report_status": rep,
                "fa_sum": fa,
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
                        ],
                        start=1,
                    ) if flag
                ],
            },

            "frequency": {
                "setpoint_before_rfg_hz": freq_set_before,
                "actual_filtered_hz": freq_actual,
                "min_hz": f_min,
                "max_hz": f_max,
            },

            "voltage": {
                "u_out_v": u_out,
            },

            "pid": {
                "setpoint_after_rfg": pid_set_after,
                "output": pid_out,
                "error": pid_err,
                "hibernation_setpoint_pct": pid_hib,
            },

            "operating_hours": {
                "motor1_h": h_m1,
                "motor2_h": h_m2,
            },

            "raw_params": raw_params,  # на випадок дебагу/лонг-логів
        }


def parse_r0052(status_word) -> dict:
    """
    Парсить r0052 (CO/BO: Active status word 1, U16) в набір прапорців.
    """
    status_word = int(status_word)

    def bit(n: int) -> int:
        return (status_word >> n) & 0x1

    # Активні-високі біти (1 = активний стан)
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

    # Активні-низькі біти (0 = активний стан)
    off2_active = (bit(4) == 0)
    off3_active = (bit(5) == 0)
    current_torque_limit_warn = (bit(11) == 0)
    motor_overload = (bit(13) == 0)
    converter_overload = (bit(15) == 0)

    # Deviation setpoint/act.value: 1 = No, 0 = Yes → активний стан, коли біт = 0
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


if __name__ == "__main__":
    client = SinamicsV20Client("192.168.1.1", 80, "/")

    client.connect()

    ident = client.query_ident()
    print("IDENT:")
    pprint(ident)

    status = client.report_status()
    print("STATUS:")
    pprint(status)

    fa = client.fa_sum()
    print("FA:")
    pprint(fa)


    state = client.read_station_state()
    pprint(state, indent=2)

    # params_to_watch = list(client.parsers.keys())
    #
    # def print_callback(data):
    #     for param, value in data.items():
    #         parser = client.parsers.get(param)
    #         raw = value.get("value_raw")
    #         if parser and raw is not None:
    #             try:
    #                 value["parsed_value"] = parser(raw)
    #             except Exception as e:
    #                 value["parsed_value"] = None
    #                 value["parse_error"] = str(e)
    #     pprint(data, indent=2)
    #
    # client.monitor(interval_sec=5.0, params=params_to_watch, callback=print_callback)
    client.close()