# eink-server for Home Assistant

Run Joan/Visionect e-ink tablets directly from Home Assistant. The
integration listens for tablets on TCP 11113, shows any image on them, and
turns taps on the screen into Home Assistant events.

> [!IMPORTANT]
> This project has been heavily vibe coded with guidance from a human.
> The generated code has been accepted without line-by-line human validation.
> The protocol was reverse engineered from packet captures, so parts of its
> behavior may remain unknown or incomplete.

```text
hass-eink-dashboard ─image entity─> automation ─eink_server.upload_image─┐
                                                                          v
                  entities <── Hub ── Gateway :11113 <──PV3/TCP── Joan tablet
```

## Features

- **Any image source**: media items, camera and image entities, or URLs.
- **Partial updates**: only the changed area is sent and redrawn.
- **Touch events**: taps arrive with upright pixel coordinates.
- **Telemetry**: battery, temperature, humidity, last seen, connectivity.
- **Delivery tracking**: from pending to confirmed on the panel.
- **Restart-safe**: the current image is kept and restored.
- **No extra server**: everything runs inside Home Assistant.

## Install

### HACS

1. In HACS, open *⋮ → Custom repositories* and add
   `https://github.com/gyger/eink-server-homeassistant` as **Integration**.
2. Download **eink-server** and restart Home Assistant.

### Manual

Copy `custom_components/eink_server` into `config/custom_components/` and
restart Home Assistant.

### Setup

1. *Settings → Devices & services → Add integration → eink-server*, port
   11113.
2. Point the tablet at Home Assistant's LAN address, port 11113, with
   outbound encryption disabled.

Each tablet appears as a device after its first check-in.

Requires Home Assistant 2026.10 or newer.

## Entities per tablet

| Entity | Description |
|---|---|
| Content (image) | Preview of the current frame |
| Touch (event) | `tap` with `x`, `y` and the displayed `frame_id` |
| Battery, Temperature, Humidity | Tablet telemetry |
| Last seen | Time of the latest check-in |
| Delivery | `pending` → `sent` → `acknowledged` → `delivered` |
| Connectivity | Tablet currently connected |

## Action: `eink_server.upload_image`

| Field | Values | Default |
|---|---|---|
| `device_id` | Target tablet | |
| `image` | Media item, camera/image entity, or allowlisted URL | |
| `fit` | `contain`, `cover`, `stretch` | `contain` |
| `rotation` | `0`, `90`, `180`, `270`, clockwise | `0` |
| `refresh` | `partial`, `full` | `partial` |

The newest image is delivered as soon as the tablet checks in. `full`
redraws the whole panel.

## Dashboards with hass-eink-dashboard

[hass-eink-dashboard](https://github.com/cryptomilk/hass-eink-dashboard)
renders Home Assistant widgets into an image. Use the *Custom* preset at the
panel resolution (Joan 6: 1024×758) with **16 display levels**; its gray
levels match the panel exactly.

```yaml
alias: Kitchen tablet dashboard
triggers:
  - trigger: state
    entity_id: image.kitchen_dashboard
actions:
  - action: eink_server.upload_image
    data:
      device_id: 0123456789abcdef0123456789abcdef
      image:
        media_content_id: media-source://image/image.kitchen_dashboard
        media_content_type: image/png
mode: restart
```

## Architecture

| Module | Role |
|---|---|
| `pv3.py` | Protocol-v3 wire codec |
| `frame.py` | Image → 16-level panel frame, changed rectangle |
| `gateway.py` | Tablet sessions and delivery |
| `hub.py` | Home Assistant state and persistence |
| platforms, `services.py` | Entities and actions |

The protocol layer is a Python port of the
[eink-server](https://github.com/gyger/eink-server) gateway.

## Development

```sh
uv sync
uv run pytest
```
