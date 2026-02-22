# Home Assistant Add-on: Sinamics V20 Pump Station

Read Siemens Sinamics V20 Smart Access values over WebSocket and publish them to
MQTT for Home Assistant.

The add-on publishes:

- A JSON state payload to an MQTT state topic.
- Home Assistant MQTT discovery entities.
- Availability status (`online` / `offline`).
- Optional MQTT command topics for writing parameters back to the drive.

## Installation

1. Add this repository to Home Assistant.
1. Install `Sinamics V20 Pump Station`.
1. Configure the Sinamics Smart Access IP address and MQTT settings.
1. Start the add-on.
1. Check the add-on logs for connection and polling status.

## Configuration

Example configuration:

```yaml
log_level: info
host: 192.168.1.1
port: 80
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_username: mqtt_user
mqtt_password: mqtt_pass
mqtt_topic: sinamics_v20/pump_station/state
poll_interval: 10
connect_timeout: 5
read_timeout: 15
core_batch_size: 7
extra_batch_size: 6
extra_params_every: 1
param_definitions:
  - r0032:dds_float
  - r0035:dds_float
  - r0037:dds_float
  - r0039:dds_float
  - r0754:dds_float
  - r0027:dds_float
```

## Options

### `log_level`

Logging level: `debug`, `info`, `warning`, or `error`.

### `host` / `port`

IP address and TCP port of the Sinamics V20 Smart Access module.

### `mqtt_host` / `mqtt_port` / `mqtt_username` / `mqtt_password`

MQTT broker connection settings.

### `mqtt_topic`

Base MQTT state topic used for JSON payload publication.

Default:

```text
sinamics_v20/pump_station/state
```

The add-on also derives:

- Availability topic: `.../availability`
- Command topic: `.../cmd` (unless overridden)
- Command result topic: `.../cmd_result`

### `mqtt_cmd_topic`

Optional override for the command topic base. If empty, it is derived from
`mqtt_topic`.

### `poll_interval`

Delay between polling cycles in seconds.

### `connect_timeout`

TCP/WebSocket connect timeout in seconds.

### `read_timeout`

Socket read timeout in seconds after the WebSocket session is established.

### `core_batch_size`

How many core parameters are read per batch request. Lower values reduce load on
the Smart Access module but increase total cycle time.

Set to `0` to read all core parameters in a single batch.

### `extra_batch_size`

How many user-defined `param_definitions` are read per batch request.

Set to `0` to read all extra parameters in a single batch.

### `extra_params_every`

Read extra parameters every N polling cycles.

Examples:

- `1`: read extra parameters every cycle
- `2`: read extra parameters every second cycle
- `3`: read extra parameters every third cycle

### `param_definitions`

List of extra parameters to publish in `value_json.params`.

Format:

```text
<parameter>:<parser>
```

Examples:

- `r0032:dds_float`
- `r0052:r0052_status`
- `r4000:r4000_mpc`
- `P2378:dds_float`
- `r1234:int`
- `r1234:raw`

Supported parsers:

- `dds_float`
- `r0052_status`
- `r4000_mpc`
- `int`
- `float`
- `raw`

## MQTT command writes (optional)

The add-on listens on the command topic and writes drive parameters.

Topic format:

```text
<cmd_topic>/<parameter>
<cmd_topic>/<parameter>/<index>
```

Examples:

```text
sinamics_v20/pump_station/cmd/P0010
sinamics_v20/pump_station/cmd/P1234/2
```

Command result topic format:

```text
<cmd_result_topic>/<parameter>
<cmd_result_topic>/<parameter>/<index>
```

Payloads:

- Write command payload: numeric or string value
- Result payload: `ok` or `error`

## Troubleshooting

- Increase `read_timeout` if you see frequent `timed out` errors.
- Increase `poll_interval` to reduce load on the Smart Access module.
- Lower `core_batch_size` and `extra_batch_size` if the device struggles with
  large batch requests.
- Increase `extra_params_every` to poll custom parameters less often.
- Make sure only one client is actively connected to the Smart Access web
  interface if you suspect connection resets.
