import json
import os
import time
import logging
import paho.mqtt.client as mqtt

from sinamics_client import (
    SinamicsV20Client,
    parse_r0052,
    parse_dds_float,
    parse_r4000_mpc_status,
)

# Available parsers registry, configurable via add-on options/env
PARSER_REGISTRY = {
    "dds_float": parse_dds_float,
    "r0052_status": parse_r0052,
    "r4000_mpc": parse_r4000_mpc_status,
    "raw": lambda x: x,
    "int": lambda x: int(x),
    "float": lambda x: float(x),
}

# Discovery hints to enrich sensors with HA metadata.
# Note: These are example mappings. Adjust codes to match your device manual if needed.
SENSOR_HINTS = {
    # Temperature-like parameters (Celsius)
    "r0032": {
        "name": "Actual filtered power",
        "device_class": "power",
        "unit_of_measurement": "kW",
        "state_class": "measurement",
        "icon": "mdi:power-socket-it",
    },
    "r0039": {
        "name": "Energy consumpt. meter",
        "device_class": "energy",
        "unit_of_measurement": "kWh",
        "state_class": "measurement",
        "icon": "mdi:thermometer",
    },
    "r0035": {
        "name": "Actual motor temperature",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "state_class": "measurement",
        "icon": "mdi:thermometer",
    }
}

logger = logging.getLogger(__name__)


def publish_discovery_configs(mqtt_client, mqtt_topic, param_config):
    """Publish MQTT discovery configurations for Home Assistant.

    Args:
        mqtt_client: Connected Paho MQTT client.
        mqtt_topic: State topic (e.g., "sinamics_v20/pump_station/state").
        param_config: Dict mapping param code to parser name, e.g. {"r0020": "dds_float"}.
    """
    base_device = {
        "identifiers": ["sinamics_pump_station"],
        "name": "Pump Station",
        "manufacturer": "Siemens",
        "model": "Sinamics V20",
    }

    # Core sensors
    discovery = {
        "sinamics_motor1_h": {
            "component": "sensor",
            "name": "Motor 1 operating hours",
            "value_template": "{{ value_json.operating_hours.motor1_h }}",
            "unit_of_measurement": "H",
            "icon": "mdi:chart-line",
        },
        "sinamics_motor2_h": {
            "component": "sensor",
            "name": "Motor 2 operating hours",
            "value_template": "{{ value_json.operating_hours.motor2_h }}",
            "unit_of_measurement": "H",
            "icon": "mdi:chart-line",
        },
        "sinamics_pump_state": {
            "component": "sensor",
            "name": "Pump Station State",
            "value_template": "{{ value_json.high_level.state }}",
            "icon": "mdi:pump",
        },
        "sinamics_pump_fault": {
            "component": "binary_sensor",
            "name": "Pump Station Fault",
            "value_template": "{{ value_json.high_level.has_fault }}",
            "device_class": "problem",
        },
        "sinamics_pump_warning": {
            "component": "binary_sensor",
            "name": "Pump Station Warning",
            "value_template": "{{ value_json.high_level.has_warning }}",
            "icon": "mdi:alert",
        },
        "sinamics_pump_freq_actual": {
            "component": "sensor",
            "name": "Actual Frequency",
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
        "sinamics_pump_freq_setpoint": {
            "component": "sensor",
            "name": "Pump Setpoint Frequency",
            "value_template": "{{ value_json.frequency.setpoint_before_rfg_hz }}",
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
        }
    }

    # Sensors for configured param_definitions
    for code in param_config.keys():
        uid = f"sinamics_param_{code}"
        discovery[uid] = {
            "component": "sensor",
            "name": f"Sinamics {code}",
            "value_template": f"{{{{ value_json.params.{code}.parsed }}}}",
            "icon": "mdi:code-braces",
        }
        # Apply HA discovery hints for known temperature/current parameters
        hints = SENSOR_HINTS.get(code)
        if hints:
            discovery[uid].update(hints)

    # Publish discovery entries
    for uid, cfg in discovery.items():
        component = cfg.pop("component")
        discovery_topic = f"homeassistant/{component}/{uid}/config"

        payload = {
            "name": cfg.pop("name"),
            "unique_id": uid,
            "state_topic": mqtt_topic,
            "device": base_device,
        }
        payload.update(cfg)

        mqtt_client.publish(discovery_topic, json.dumps(payload), retain=True)
        logger.info("Published discovery: %s", discovery_topic)

def _normalize_param_items(items) -> dict:
    """Normalize iterable of 'CODE[:PARSER]' strings into dict.

    Args:
        items: Iterable of strings. Each string is either 'CODE:PARSER' or 'CODE'.

    Returns:
        Dict mapping 'CODE' to 'PARSER' (default 'raw' when not provided).
    """
    result = {}
    for item in items:
        if not isinstance(item, str):
            continue
        if ":" in item:
            code, parser_name = item.split(":", 1)
        else:
            code, parser_name = item.strip(), "raw"
        code = code.strip()
        parser_name = parser_name.strip()
        if code:
            result[code] = parser_name
    return result


def _parse_param_defs_string(raw: str) -> dict:
    """Parse PARAM_DEFS from JSON or from newline/comma separated text.

    Accepted formats:
      - JSON array of strings: ["r0052:r0052_status", "r4000:r4000_mpc"]
      - Plain text with one 'CODE:PARSER' per line (or comma-separated).
      - Lines starting with '#' are ignored.

    Args:
        raw: Raw value from the PARAM_DEFS environment variable.

    Returns:
        Dict mapping code -> parser_name.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}

    # Try JSON first if it looks like JSON content
    if raw[0] in "[{":
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                logger.warning("PARAM_DEFS JSON is not a list, got: %r", type(items))
                return {}
            return _normalize_param_items(items)
        except Exception as exc:
            logger.warning("Failed to parse PARAM_DEFS as JSON: %s", exc)
            # Fall through to plain-text parsing

    # Fallback plain-text parsing: split by lines; if single line, allow commas
    lines = raw.splitlines()
    if len(lines) == 1:
        lines = [part for part in raw.split(",")]

    items = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        items.append(s)

    return _normalize_param_items(items)

def load_param_config_from_env() -> dict:
    """Read and parse PARAM_DEFS from environment only.

    Supported formats:
      - JSON array of strings
      - Newline- or comma-separated 'CODE:PARSER' entries
    """
    raw = os.getenv("PARAM_DEFS", "")
    if not raw.strip():
        logger.info("PARAM_DEFS is empty; no parameters will be polled.")
        return {}

    param_config = _parse_param_defs_string(raw)
    if not param_config:
        logger.warning("PARAM_DEFS could not be parsed; no parameters will be polled.")
        return {}

    logger.info("Loaded param definitions: %s", list(param_config.keys()))
    return param_config


def main():
    """Entrypoint for the add-on bridge: poll device and publish to MQTT."""
    # Configure logging once in the entrypoint (stdout for add-on)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    host = os.getenv("SINAMICS_HOST", "192.168.1.1")
    port = int(os.getenv("SINAMICS_PORT", "80"))

    mqtt_host = os.getenv("MQTT_HOST", "core-mosquitto")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_username = os.getenv("MQTT_USERNAME", "")
    mqtt_password = os.getenv("MQTT_PASSWORD", "")
    mqtt_topic = os.getenv("MQTT_TOPIC", "sinamics_v20/pump_station/state")
    poll_interval = float(os.getenv("POLL_INTERVAL", "5"))

    param_config = load_param_config_from_env()
    param_codes = list(param_config.keys())

    logger.info("Sinamics host: %s:%s", host, port)
    logger.info(
        "MQTT: %s:%s, topic=%s, interval=%ss", mqtt_host, mqtt_port, mqtt_topic, poll_interval
    )
    logger.info("Parameters to poll: %s", param_codes)

    client = SinamicsV20Client(host, port, "/")

    mqtt_client = mqtt.Client()
    if mqtt_username:
        mqtt_client.username_pw_set(mqtt_username, mqtt_password)
    try:
        mqtt_client.connect(mqtt_host, mqtt_port, 60)
        mqtt_client.loop_start()
        # Publish discovery configs
        publish_discovery_configs(mqtt_client, mqtt_topic, param_config)
    except Exception:
        logger.exception("Connection to MQTT failed.")

    try:
        client.connect()
        while True:
            try:
                # Read aggregated station state
                base_state = client.read_station_state()

                # Read additional configured parameters
                extra_raw = client.read_params_batch(param_codes) if param_codes else {}

                extra_parsed = {}
                for code, meta in extra_raw.items():
                    raw_val = meta.get("value_raw")
                    parser_name = param_config.get(code, "raw")
                    parser_fn = PARSER_REGISTRY.get(parser_name)

                    if parser_fn is None:
                        parsed = raw_val
                    else:
                        try:
                            parsed = parser_fn(raw_val)
                        except Exception as exc:
                            parsed = {"raw": raw_val, "parse_error": str(exc)}
                            logger.warning(
                                "Parsing error for %s with parser %s: %s",
                                code,
                                parser_name,
                                exc,
                            )

                    extra_parsed[code] = {
                        "raw": raw_val,
                        "parsed": parsed,
                        "status": meta.get("status"),
                        "index": meta.get("index"),
                    }

                # Attach configured params section
                base_state["params"] = extra_parsed

                payload = json.dumps(base_state, default=str)
                mqtt_client.publish(mqtt_topic, payload, qos=0, retain=False)
                logger.debug("Published state to %s", mqtt_topic)
                time.sleep(poll_interval)
            except Exception:
                logger.exception("Error reading/publishing state")
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        client.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        logger.info("Cleaned up and disconnected")


if __name__ == "__main__":
    main()