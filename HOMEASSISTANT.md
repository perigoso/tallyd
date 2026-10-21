# Home Assistant

Push the "in call" state into [Home Assistant](https://www.home-assistant.io) as a `binary_sensor`, so that a
tally light at the door (or anything else) can be set accordingly.

This integration is configuration only. No custom integration, long-lived
token, MQTT broker, etc, tallyd's call actions `curl` to a Home Assistant webhook,
and a trigger-based template entity turns the webhook into a sensor. The
entity has a `unique_id`, so it can be renamed and assigned to an area in the
UI, and its state is restored when Home Assistant restarts.

## 1. Pick a webhook id

The id is the only secret, so make it long and random:

```console
$ uuidgen
7f3a9c2e-1b4d-4e0a-9d1c-2a6f8b3e5c71
```

Webhooks are reachable from the local network only, and accept `POST` only,
by default. If you want this to work remotely those need configuration.

## 2. Home Assistant

Add to `configuration.yaml` (syntax for Home Assistant 2024.10 or newer):

```yaml
template:
  - triggers:
      - trigger: webhook
        webhook_id: tallyd-7f3a9c2e-1b4d-4e0a-9d1c-2a6f8b3e5c71
    binary_sensor:
      - name: "In call"
        unique_id: tallyd_in_call
        icon: mdi:phone-in-talk
        state: "{{ trigger.data.state == 'active' }}"
        attributes:
          sources: "{{ trigger.data.sources }}"
```

Reload it (no restart needed): Developer tools, YAML tab, "Template entities".
Trigger-based template entities are not offered by the UI helper editor, so
this setup needs to be done in YAML.

## 3. tallyd

In `~/.config/tallyd/tallyd.conf`. The same command serves both edges, because
`TALLYD_STATE` carries the direction. Replace the host with your instance:

```toml
[actions]
on_call_start = 'curl -sS -m 5 --data-urlencode "state=$TALLYD_STATE" --data-urlencode "sources=$TALLYD_SOURCES" http://homeassistant.local:8123/api/webhook/tallyd-7f3a9c2e-1b4d-4e0a-9d1c-2a6f8b3e5c71'
on_call_end   = 'curl -sS -m 5 --data-urlencode "state=$TALLYD_STATE" --data-urlencode "sources=$TALLYD_SOURCES" http://homeassistant.local:8123/api/webhook/tallyd-7f3a9c2e-1b4d-4e0a-9d1c-2a6f8b3e5c71'
```

`--data-urlencode` is not optional: sources look like
`camera:/dev/video0(firefox[4182])` and contain characters a bare `-d` would
mangle. `-m 5` stops a dead Home Assistant from leaving `curl` processes
behind; actions are spawned and never waited on, so detection is unaffected
either way.

## 4. Verify

Fire the webhook by hand before trusting the daemon with it:

```console
$ curl -sS --data-urlencode state=active --data-urlencode sources=test \
    http://homeassistant.local:8123/api/webhook/tallyd-7f3a9c2e-1b4d-4e0a-9d1c-2a6f8b3e5c71
```

`binary_sensor.in_call` now reads `on` under Developer tools, States, with
`sources: test`. Send `state=inactive` to clear it. Then restart tallyd and
open the microphone in anything:

```console
$ systemctl --user restart tallyd
$ journalctl --user -fu tallyd
```

## 5. Use it

The tally light from the README, as an automation. The UI editor builds the
same thing:

```yaml
automation:
  - alias: "Tally light"
    triggers:
      - trigger: state
        entity_id: binary_sensor.in_call
        to: ["on", "off"]
    actions:
      - action: "light.turn_{{ trigger.to_state.state }}"
        target:
          entity_id: light.office_door
```

## Limits

- tallyd fires on edges only. A clean stop while in a call sends the end
  state; a crash or `SIGKILL` does not, and the sensor stays `on` until the
  next call ends.
- A transition that happens while Home Assistant is down is lost. The sensor
  catches up at the next status.
- `sources` is empty on the end status.
