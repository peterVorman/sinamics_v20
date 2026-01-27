import json
import os
import socket
import time
import logging
import paho.mqtt.client as mqtt

from sinamics_client import (
    SinamicsV20Client,
    parse_r0052,
    parse_dds_float,
    parse_r4000_mpc_status,
)

# Available parsers registry
PARSER_REGISTRY = {
    "dds_float": parse_dds_float,
    "r0052_status": parse_r0052,
    "r4000_mpc": parse_r4000_mpc_status,
    "raw": lambda x: x,
    "int": lambda x: int(x),
    "float": lambda x: float(x),
}

# Discovery hints to enrich sensors with HA metadata.
SENSOR_HINTS = {
    # --- Power & Energy ---
    "r0032": {
        "name": "Actual power",
        "device_class": "power",
        "unit_of_measurement": "kW",
        "state_class": "measurement",
        "icon": "mdi:power-socket-it",
    },
    "r0039": {
        "name": "Energy consumpt. meter",
        "device_class": "energy",
        "unit_of_measurement": "kWh",
        "state_class": "total_increasing",
        "icon": "mdi:meter-electric",
    },
    
    # --- Temperatures & Load ---
    "r0035": {
        "name": "Motor temperature (Calc)", 
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "state_class": "measurement",
        "icon": "mdi:engine",
    },
    "r0037": {
        "name": "Inverter temperature",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "state_class": "measurement",
        "icon": "mdi:chip",
    },
    "r0036": {
        "name": "Inverter Overload Util (I2t)",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:fire-alert",
    },

    # --- IO & Current ---
    "r0754": {
        "name": "Analog input value",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:chart-line",
    },
    "r0027": {
        "name": "Output current",
        "unit_of_measurement": "A",
        "state_class": "measurement",
        "icon": "mdi:chart-line",
    },

    # --- Logic & Diagnostics ---
    "P2200": {"name": "PID Enable Status", "icon": "mdi:check-network-outline"},
    "P2370": {"name": "Motor Staging Enable", "icon": "mdi:engine-outline"},
    "P2372": {"name": "Motor Alternation Mode", "icon": "mdi:swap-horizontal"},
    "P2378": {
        "name": "Motor Staging Frequency",
        "unit_of_measurement": "Hz",
        "state_class": "measurement",
        "icon": "mdi:sine-wave",
    },
    "r4000": {"name": "Multi-Pump Status", "icon": "mdi:robot-industrial"},

    # --- STATUS WORDS (Bit Expansion) ---
    "r0052": {
        "name": "Status Word 1",
        "icon": "mdi:list-status",
        "bitmask": {
            0: "Ready to switch on",
            1: "Ready to operate",
            2: "Operation enabled",
            3: "Fault present",
            4: "OFF2 inactive",
            5: "OFF3 inactive",
            6: "Switch-on inhibited",
            7: "Alarm present",
            8: "Deviation set/act",
            9: "PZD control",
            10: "f_max reached",
            11: "V/I Limit Active",
            12: "Motor holding brake",
            13: "Motor overload",
            14: "Motor Direction Right",
            15: "Inverter overload",
        }
    },
    "r0053": {
        "name": "Status Word 2",
        "icon": "mdi:list-status",
        "bitmask": {
            0: "DC brake active",
            1: "f_act > P2167",
            2: "f_act < P2167",
            3: "f_act > f_max",
            6: "f_set reached",
            7: "Vdc_act > r1242 (Vdc_max)",
            8: "Vdc_act < P2172 (Vdc_min)",
            9: "Ramping finished",
            10: "PID output min limit",
            11: "PID output max limit",
        }
    },
    "r0054": {
        "name": "Control Word 1",
        "icon": "mdi:controller",
        "bitmask": {
            0: "ON/OFF1",
            1: "OFF2",
            2: "OFF3",
            3: "Pulse enable",
            4: "RFG enable",
            5: "RFG start",
            6: "Setpoint enable",
            7: "Fault acknowledge",
            8: "JOG right",
            9: "JOG left",
            10: "Control from PLC",
            11: "Reverse (Setpoint inversion)",
            13: "MotorPot UP",
            14: "MotorPot DOWN",
        }
    },
    "r0056": {
        "name": "Status Motor Control",
        "icon": "mdi:engine-settings",
        "bitmask": {
            0: "Init. energized",
            1: "Magnetizing",
            2: "Running",
            3: "Active",
            4: "Hold",
            5: "Ramp-up",
            6: "Ramp-down",
            7: "Synergy enabled",
            10: "Flying start active",
            11: "DC braking active",
            12: "Unknown 12",
            13: "Jogging active",
            14: "Flying start OK",
        }
    },
    "r1204": {
        "name": "Status Vdc Controller",
        "icon": "mdi:lightning-bolt",
        "bitmask": {
            0: "Vdc-max controller inactive",
            1: "Vdc-max controller active",
        }
    },
    "r1348": {
        "name": "Status I-max Controller",
        "icon": "mdi:current-ac",
        "bitmask": {
            0: "I-max controller output active",
        }
    },
    "r2399": {
        "name": "Status Energy Saving",
        "icon": "mdi:leaf",
        "bitmask": {
            0: "Energy saving active",
            1: "Energy saving controller active",
        }
    }
}

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "100"))      # per poll cycle
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "5"))  # seconds

logger = logging.getLogger(__name__)


def publish_discovery_configs(mqtt_client, mqtt_topic, param_config, availability_topic):
    """Publish MQTT discovery configurations for Home Assistant."""
    base_device = {
        "identifiers": ["sinamics_pump_station"],
        "name": "Pump Station",
        "manufacturer": "Siemens",
        "model": "Sinamics V20",
    }

    # --- CORE SENSORS ---
    discovery = {
        "sinamics_motor1_h": {
            "component": "sensor",
            "name": "Motor 1 operating hours",
            "value_template": "{{ value_json.operating_hours.motor1_h }}",
            "unit_of_measurement": "h",
            "icon": "mdi:clock-outline",
        },
        "sinamics_motor2_h": {
            "component": "sensor",
            "name": "Motor 2 operating hours",
            "value_template": "{{ value_json.operating_hours.motor2_h }}",
            "unit_of_measurement": "h",
            "icon": "mdi:clock-outline",
        },
        "sinamics_pump_state": {
            "component": "sensor",
            "name": "Station State",
            "value_template": "{{ value_json.high_level.state }}",
            "icon": "mdi:pump",
        },
        "sinamics_pump_fault": {
            "component": "binary_sensor",
            "name": "Station Fault",
            "value_template": "{{ value_json.high_level.has_fault }}",
            "device_class": "problem",
        },
        "sinamics_pump_warning": {
            "component": "binary_sensor",
            "name": "Station Warning",
            "value_template": "{{ value_json.high_level.has_warning }}",
            "icon": "mdi:alert",
        },
        "sinamics_pump_freq_actual": {
            "component": "sensor",
            "name": "Frequency Actual",
            "value_template": "{{ value_json.frequency.actual_filtered_hz }}",
            "unit_of_measurement": "Hz",
            "icon": "mdi:sine-wave",
        },
        "sinamics_pump_setpoint_before_rfg_hz": {
            "component": "sensor",
            "name": "Frequency setpoint",
            "value_template": "{{ value_json.frequency.setpoint_before_rfg_hz }}",
            "unit_of_measurement": "Hz",
            "icon": "mdi:sine-wave",
        },
        "sinamics_pump_freq_min": {
            "component": "sensor",
            "name": "Frequency min",
            "value_template": "{{ value_json.frequency.min_hz }}",
            "unit_of_measurement": "Hz",
            "icon": "mdi:sine-wave",
        },
        "sinamics_pump_freq_max": {
            "component": "sensor",
            "name": "Frequency max",
            "value_template": "{{ value_json.frequency.max_hz }}",
            "unit_of_measurement": "Hz",
            "icon": "mdi:sine-wave",
        },
        "sinamics_pump_voltage": {
            "component": "sensor",
            "name": "Pump Output Voltage",
            "value_template": "{{ value_json.voltage.u_out_v }}",
            "unit_of_measurement": "V",
            "icon": "mdi:alpha-v-circle-outline",
        },
        "sinamics_pump_pid_output": {
            "component": "sensor",
            "name": "PID Output",
            "value_template": "{{ value_json.pid.output }}",
            "unit_of_measurement": "%",
            "icon": "mdi:chart-line",
        },
        "sinamics_pump_pid_setpoint_after_rfg": {
            "component": "sensor",
            "name": "PID Setpoint",
            "value_template": "{{ value_json.pid.setpoint_after_rfg }}",
            "unit_of_measurement": "%",
            "icon": "mdi:chart-line",
        },
        "sinamics_pump_pid_error": {
            "component": "sensor",
            "name": "PID error",
            "value_template": "{{ value_json.pid.error }}",
            "unit_of_measurement": "%",
            "icon": "mdi:chart-line",
        },
        "sinamics_pump_pid_hibernation_setpoint_pct": {
            "component": "sensor",
            "name": "PID hibernation setpoint",
            "value_template": "{{ value_json.pid.hibernation_setpoint_pct }}",
            "unit_of_measurement": "%",
            "icon": "mdi:chart-line",
        },
        "sinamics_pump_running_motors": {
            "component": "sensor",
            "name": "Pump Running Motors",
            "value_template": "{{ value_json.multi_pump.running_motors | join(',') }}",
            "icon": "mdi:pump",
        },
        # FIX: Value template is now a valid Python string
        "sinamics_pump_efficiency": {
            "component": "sensor",
            "name": "Pump Efficiency Factor",
            "value_template": (
                "{% if value_json.params.r0032 is defined and value_json.frequency.actual_filtered_hz | float(0) > 10 %}"
                "{{ (value_json.params.r0032.parsed | float(0) * 1000 / (value_json.frequency.actual_filtered_hz | float(1))) | round(1) }}"
                "{% else %}0{% endif %}"
            ),
            "unit_of_measurement": "W/Hz",
            "icon": "mdi:chart-bell-curve-cumulative",
        }
    }

    # Publish Core Sensors
    for uid, cfg in discovery.items():
        component = cfg.pop("component")
        discovery_topic = f"homeassistant/{component}/{uid}/config"
        payload = {
            "name": cfg.pop("name"),
            "unique_id": uid,
            "state_topic": mqtt_topic,
            "device": base_device,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        payload.update(cfg)
        mqtt_client.publish(discovery_topic, json.dumps(payload), retain=True)

    # --- DYNAMIC PARAMETERS ---
    for code in param_config.keys():
        hints = SENSOR_HINTS.get(code, {})
        
        # 1. Main Sensor (Integer/Value)
        uid = f"sinamics_param_{code}"
        payload = {
            "name": hints.get("name", f"Sinamics {code}"),
            "unique_id": uid,
            "state_topic": mqtt_topic,
            "value_template": f"{{{{ value_json.params.{code}.parsed }}}}",
            "device": base_device,
            "availability_topic": availability_topic,
            "icon": hints.get("icon", "mdi:code-braces"),
        }
        if "device_class" in hints: payload["device_class"] = hints["device_class"]
        if "unit_of_measurement" in hints: payload["unit_of_measurement"] = hints["unit_of_measurement"]
        if "state_class" in hints: payload["state_class"] = hints["state_class"]
        
        discovery_topic = f"homeassistant/sensor/{uid}/config"
        mqtt_client.publish(discovery_topic, json.dumps(payload), retain=True)

        # 2. Bitmask Expansion (Binary Sensors)
        if "bitmask" in hints:
            for bit, label in hints["bitmask"].items():
                bit_uid = f"sinamics_param_{code}_bit{bit}"
                # Use bitwise_and to extract bit state
                bit_payload = {
                    "name": f"{hints.get('name', code)}: {label}",
                    "unique_id": bit_uid,
                    "state_topic": mqtt_topic,
                    "value_template": f"{{{{ 'ON' if (value_json.params.{code}.raw | int(0) | bitwise_and({1 << bit})) > 0 else 'OFF' }}}}",
                    "device": base_device,
                    "availability_topic": availability_topic,
                    "icon": "mdi:checkbox-marked-circle-outline",
                }
                
                # Special classes for common bits
                if "Fault" in label or "Alarm" in label or "Overload" in label:
                    bit_payload["device_class"] = "problem"
                elif "Ready" in label:
                    bit_payload["icon"] = "mdi:check-circle"
                elif "Running" in label or "Active" in label:
                    bit_payload["icon"] = "mdi:play-circle"

                bit_topic = f"homeassistant/binary_sensor/{bit_uid}/config"
                mqtt_client.publish(bit_topic, json.dumps(bit_payload), retain=True)
                
    logger.info("Published discovery configs")


def _normalize_param_items(items) -> dict:
    result = {}
    for item in items:
        if not isinstance(item, str): continue
        if ":" in item:
            code, parser_name = item.split(":", 1)
        else:
            code, parser_name = item.strip(), "raw"
        code = code.strip()
        parser_name = parser_name.strip()
        if code: result[code] = parser_name
    return result

def _parse_param_defs_string(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw: return {}
    if raw[0] in "[{":
        try:
            items = json.loads(raw)
            if not isinstance(items, list): return {}
            return _normalize_param_items(items)
        except: pass
    lines = raw.splitlines()
    if len(lines) == 1: lines = [part for part in raw.split(",")]
    items = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"): continue
        items.append(s)
    return _normalize_param_items(items)

def load_param_config_from_env() -> dict:
    raw = os.getenv("PARAM_DEFS", "")
    if not raw.strip(): return {}
    return _parse_param_defs_string(raw)

def _retry_sleep(attempt: int, base: float = 1.0, cap: float = 60.0) -> None:
    delay = min(base * (2 ** (attempt - 1)), cap)
    logger.warning("Retry attempt %d – sleeping %.1fs …", attempt, delay)
    time.sleep(delay)

def main():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    host = os.getenv("SINAMICS_HOST", "192.168.1.1")
    port = int(os.getenv("SINAMICS_PORT", "80"))
    mqtt_host = os.getenv("MQTT_HOST", "core-mosquitto")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_username = os.getenv("MQTT_USERNAME", "")
    mqtt_password = os.getenv("MQTT_PASSWORD", "")
    mqtt_topic = os.getenv("MQTT_TOPIC", "sinamics_v20/pump_station/state")
    poll_interval = float(os.getenv("POLL_INTERVAL", "5"))

    if mqtt_topic.endswith("/state"):
        availability_topic = mqtt_topic.replace("/state", "/availability")
    else:
        availability_topic = f"{mqtt_topic}/availability"

    param_config = load_param_config_from_env()
    param_codes = list(param_config.keys())

    client = SinamicsV20Client(host, port, "/")

    if hasattr(mqtt, "CallbackAPIVersion"):
        mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    else:
        mqtt_client = mqtt.Client()
        
    if mqtt_username: mqtt_client.username_pw_set(mqtt_username, mqtt_password)
    mqtt_client.will_set(availability_topic, "offline", retain=True)

    try:
        mqtt_client.connect(mqtt_host, mqtt_port, 60)
        mqtt_client.loop_start()
        mqtt_client.publish(availability_topic, "online", retain=True)
        publish_discovery_configs(mqtt_client, mqtt_topic, param_config, availability_topic)
    except Exception:
        logger.exception("Connection to MQTT failed.")

    consec_errors = 0

    while True:
        try:
            if client.sock is None:
                logger.info("WebSocket connecting …")
                client.connect()

            base_state = client.read_station_state()
            extra_raw = client.read_params_batch(param_codes) if param_codes else {}

            extra_parsed = {}
            for code, meta in extra_raw.items():
                raw_val = meta.get("value_raw")
                parser_name = param_config.get(code, "raw")
                parser_fn = PARSER_REGISTRY.get(parser_name)
                parsed = parser_fn(raw_val) if parser_fn else raw_val
                extra_parsed[code] = {"raw": raw_val, "parsed": parsed}

            base_state["params"] = extra_parsed
            payload = json.dumps(base_state, default=str)
            
            if mqtt_client.is_connected():
                mqtt_client.publish(mqtt_topic, payload, qos=0, retain=False)
            consec_errors = 0
            time.sleep(poll_interval)

        except (TimeoutError, OSError, RuntimeError, socket.error) as exc:
            consec_errors += 1
            logger.error("Polling error (%d/%d): %s", consec_errors, MAX_RETRIES, exc)
            client.close()
            if consec_errors >= MAX_RETRIES:
                try: mqtt_client.publish(availability_topic, "offline", retain=True)
                except: pass
                raise
            _retry_sleep(consec_errors, base=BASE_BACKOFF)
        except Exception:
            consec_errors += 1
            logger.exception("Unhandled error")
            client.close()
            if consec_errors >= MAX_RETRIES: raise
            _retry_sleep(consec_errors, base=BASE_BACKOFF)

if __name__ == "__main__":
    main()
