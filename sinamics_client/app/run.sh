#!/usr/bin/with-contenv bashio
set -e

HOST=$(bashio::config 'host')
PORT=$(bashio::config 'port')
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USERNAME=$(bashio::config 'mqtt_username')
MQTT_PASSWORD=$(bashio::config 'mqtt_password')
MQTT_TOPIC=$(bashio::config 'mqtt_topic')
MQTT_CMD_TOPIC=$(bashio::config 'mqtt_cmd_topic')
POLL_INTERVAL=$(bashio::config 'poll_interval')
CONNECT_TIMEOUT=$(bashio::config 'connect_timeout')
READ_TIMEOUT=$(bashio::config 'read_timeout')
CORE_BATCH_SIZE=$(bashio::config 'core_batch_size')
EXTRA_BATCH_SIZE=$(bashio::config 'extra_batch_size')
EXTRA_PARAMS_EVERY=$(bashio::config 'extra_params_every')
PARAM_DEFS=$(bashio::config 'param_definitions')
LOG_LEVEL=$(bashio::config 'log_level')

export LOG_LEVEL="$LOG_LEVEL"
export PARAM_DEFS="$PARAM_DEFS"
export SINAMICS_HOST="$HOST"
export SINAMICS_PORT="$PORT"
export MQTT_HOST="$MQTT_HOST"
export MQTT_PORT="$MQTT_PORT"
export MQTT_USERNAME="$MQTT_USERNAME"
export MQTT_PASSWORD="$MQTT_PASSWORD"
export MQTT_TOPIC="$MQTT_TOPIC"
export MQTT_CMD_TOPIC="$MQTT_CMD_TOPIC"
export POLL_INTERVAL="$POLL_INTERVAL"
export CONNECT_TIMEOUT="$CONNECT_TIMEOUT"
export READ_TIMEOUT="$READ_TIMEOUT"
export CORE_BATCH_SIZE="$CORE_BATCH_SIZE"
export EXTRA_BATCH_SIZE="$EXTRA_BATCH_SIZE"
export EXTRA_PARAMS_EVERY="$EXTRA_PARAMS_EVERY"

echo "Starting Sinamics V20 bridge..."
exec python3 -u bridge.py
