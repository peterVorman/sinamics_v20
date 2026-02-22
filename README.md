# Home Assistant Add-ons: Sinamics V20

Custom Home Assistant add-on repository for integrating a Siemens Sinamics V20
drive (via Smart Access) with MQTT and Home Assistant.

## Repository URL

Use this URL in Home Assistant Add-on Store -> Repositories:

```text
https://github.com/peterVorman/sinamics_v20
```

## Included Add-ons

### `Sinamics V20 Pump Station` (`sinamics_v20`)

Reads Sinamics V20 Smart Access values over WebSocket and publishes:

- MQTT JSON state payloads
- Home Assistant MQTT discovery entities
- Availability state (`online` / `offline`)
- Optional MQTT command topics for parameter writes

Add-on docs: [`sinamics_client/DOCS.md`](sinamics_client/DOCS.md)

## Features

- Supports `amd64` and `aarch64`
- Configurable MQTT broker and topics
- Configurable polling interval and socket timeouts
- Chunked parameter polling to reduce device load
- Optional extra parameter parsing (`dds_float`, `r0052_status`, `r4000_mpc`)
- MQTT write commands with result topics

## Installation (Repository)

1. In Home Assistant, go to `Settings` -> `Add-ons` -> `Add-on Store`.
1. Open the menu in the top-right corner and choose `Repositories`.
1. Add `https://github.com/peterVorman/sinamics_v20`.
1. Install `Sinamics V20 Pump Station`.
1. Open the add-on documentation for configuration details.

## Development Notes

- The add-on `config.yaml` currently uses `version: dev` on `main` to satisfy the
  Home Assistant add-on linter.
- CI validates markdown/yaml formatting and add-on metadata.

## Support

- Open an issue in this repository for bugs or feature requests.
- Include add-on logs and your configuration (without secrets) when reporting
  connection/polling issues.

## License

MIT License. See [`LICENSE.md`](LICENSE.md).
