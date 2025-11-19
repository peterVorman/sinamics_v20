import json
import os
import time
import paho.mqtt.client as mqtt

from sinamics_client import (
    SinamicsV20Client,
    parse_r0052,
    parse_dds_float,
    parse_r4000_mpc_status,
)

# Доступні парсери, якими можна оперувати з конфіга
PARSER_REGISTRY = {
    "dds_float": parse_dds_float,
    "r0052_status": parse_r0052,
    "r4000_mpc": parse_r4000_mpc_status,
    "raw": lambda x: x,
    "int": lambda x: int(x),
    "float": lambda x: float(x),
}

def publish_discovery_configs(mqtt_client, mqtt_topic, param_config):
    """
    Publish MQTT discovery configs for Home Assistant.
    mqtt_topic: основний state_topic (наприклад: sinamics_v20/pump_station/state)
    param_config: dict {"r0020": "dds_float", ...}
    """

    base_device = {
        "identifiers": ["sinamics_pump_station"],
        "name": "Pump Station",
        "manufacturer": "Siemens",
        "model": "Sinamics V20",
    }

    # --------------- Основні сенсори ----------------

    discovery = {
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
            "name": "Pump Actual Frequency",
            "value_template": "{{ value_json.frequency.actual_filtered_hz }}",
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
            "name": "Pump PID Output",
            "value_template": "{{ value_json.pid.output }}",
            "unit_of_measurement": "%",
            "icon": "mdi:chart-line",
        },
        "sinamics_pump_running_motors": {
            "component": "sensor",
            "name": "Pump Running Motors",
            "value_template": "{{ value_json.multi_pump.running_motors | join(',') }}",
            "icon": "mdi:pump",
        },
    }

    # --------------- Сенсори з param_definitions ----------------
    # Для кожного від param_config

    for code in param_config.keys():
        uid = f"sinamics_param_{code}"
        discovery[uid] = {
            "component": "sensor",
            "name": f"Sinamics {code}",
            "value_template": f"{{{{ value_json.params.{code}.parsed }}}}",
            "icon": "mdi:code-braces",
        }

    # --------------- Публікація discovery ----------------

    for uid, cfg in discovery.items():
        component = cfg.pop("component")

        discovery_topic = f"homeassistant/{component}/{uid}/config"

        payload = {
            "name": cfg.pop("name"),
            "unique_id": uid,
            "state_topic": mqtt_topic,
            "device": base_device,
        }

        payload.update(cfg)  # додаємо value_template, icon, unit_of_measurement

        mqtt_client.publish(
            discovery_topic,
            json.dumps(payload),
            retain=True,
        )

        print(f"Published discovery: {discovery_topic}")

def load_param_config_from_env() -> dict:
    """
    Читає PARAM_DEFS з env (JSON-масив рядків 'код:парсер')
    і повертає dict { "r0052": "r0052_status", ... }.
    """
    raw = os.getenv("PARAM_DEFS", "[]")
    try:
        items = json.loads(raw)
    except Exception as e:
        print("Failed to parse PARAM_DEFS, using empty list:", e)
        items = []

    param_config = {}
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
            param_config[code] = parser_name

    print("Loaded param definitions:", param_config)
    return param_config
    
def main():
    host = os.getenv("SINAMICS_HOST", "192.168.1.1")
    port = int(os.getenv("SINAMICS_PORT", "80"))

    mqtt_host = os.getenv("MQTT_HOST", "core-mosquitto")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_username = os.getenv("MQTT_USERNAME", "")
    mqtt_password = os.getenv("MQTT_PASSWORD", "")
    mqtt_topic = os.getenv("MQTT_TOPIC", "sinamics_v20/pump_station/state")
    poll_interval = int(os.getenv("POLL_INTERVAL", "5"))

    param_config = load_param_config_from_env()
    param_codes = list(param_config.keys())

    print(f"Sinamics host: {host}:{port}")
    print(f"MQTT: {mqtt_host}:{mqtt_port}, topic={mqtt_topic}, interval={poll_interval}s")
    print(f"Parameters to poll: {param_codes}")

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
        print("Connection to MQTT failed.")

    try:
        client.connect()
        while True:
            try:
                # Базовий агрегований стан (якщо хочеш зберегти read_station_state)
                base_state = client.read_station_state()

                # Дочитуємо/або пере-читуємо параметри із конфігурації
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
                        except Exception as e:
                            parsed = {"raw": raw_val, "parse_error": str(e)}

                    extra_parsed[code] = {
                        "raw": raw_val,
                        "parsed": parsed,
                        "status": meta.get("status"),
                        "index": meta.get("index"),
                    }

                # Додаємо секцію "params" з конфігованими параметрами
                base_state["params"] = extra_parsed

                payload = json.dumps(base_state, default=str)
                mqtt_client.publish(mqtt_topic, payload, qos=0, retain=False)
                time.sleep(poll_interval)
            except Exception as e:
                print("Error reading/publishing state:", e)
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("Stopped by user")
    finally:
        client.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()



if __name__ == "__main__":
    main()