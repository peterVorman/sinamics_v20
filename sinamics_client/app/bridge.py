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

    mqtt_client.connect(mqtt_host, mqtt_port, 60)
    mqtt_client.loop_start()

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