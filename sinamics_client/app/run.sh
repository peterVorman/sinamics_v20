#!/usr/bin/with-contenv bashio
set -e

HOST=$(bashio::config 'host')
PORT=$(bashio::config 'port')
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USERNAME=$(bashio::config 'mqtt_username')
MQTT_PASSWORD=$(bashio::config 'mqtt_password')
MQTT_TOPIC=$(bashio::config 'mqtt_topic')
POLL_INTERVAL=$(bashio::config 'poll_interval')
PARAM_DEFS=$(bashio::config 'param_definitions')

export PARAM_DEFS="$PARAM_DEFS"
export SINAMICS_HOST="$HOST"
export SINAMICS_PORT="$PORT"
export MQTT_HOST="$MQTT_HOST"
export MQTT_PORT="$MQTT_PORT"
export MQTT_USERNAME="$MQTT_USERNAME"
export MQTT_PASSWORD="$MQTT_PASSWORD"
export MQTT_TOPIC="$MQTT_TOPIC"
export POLL_INTERVAL="$POLL_INTERVAL"

echo "Starting Sinamics V20 bridge..."
exec python3 bridge.py