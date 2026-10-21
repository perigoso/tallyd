# tallyd

*A tally light is the red lamp on a studio camera meaning "this one is live".*

Detects whether a call is ongoing, independently
of the platform it is happening on; Teams,
Meet, Zoom, Discord, etc.

It does not rely on any specific conferencing vendor. It watches the **operating system's
capture state**: is the microphone recording, is the camera open. Every platform
looks identical, making it agnostic.

```console
$ tallyd --check
{
  "in_call": true,
  "sources": [
    "mic:firefox",
    "camera:/dev/video0(firefox[4182])"
  ]
}
```

## Motivation

The classic. I work from home, and the camera points at a room my wife
also lives in. A tally light at the door answers the two questions that
matter before walking in: *will I be seen?* and *will I be heard?* Without
one, the answers get discovered the hard way...

The signal had to come from the operating system instead of the call platform:

- **The platform APIs are gated.** Reading your own Teams state means a
  registered app, Graph permissions and a tenant admin who consents to them.
  That is a lot of ceremony for "is my mic on".
- **It is not always Teams.** Client work means the client's tools: Meet,
  Zoom, whatever they run. A detector per platform is a detector that is
  wrong on the day it matters.
- **Vendor APIs change at the vendor's convenience.** A per-platform
  integration keeps working only as long as the vendor lets it.

Whatever the platform, a call has to open the microphone or the camera, and
on Linux that goes through (_probably_) PipeWire and `/dev/video*`. Watch there and every
platform should look the same, including ones that do not exist yet.

## How detection works

It uses two independent signals, because neither covers both devices.

| Device | Mechanism | Latency |
|---|---|---|
| **Microphone** | PipeWire graph: a `Stream/Input/Audio` node in state `running` | one poll (2 s default) |
| **Camera** | `inotify` on `/dev/video*`, confirmed against `/proc/<pid>/fd` | immediate |

PipeWire is deliberately not used for the camera. Chromium and Electron apps
(Teams, Slack, Discord), Zoom and stock Firefox open V4L2 directly and stay
invisible in the PipeWire graph the entire time. A PipeWire-only detector
reports "camera idle" during a real video call.

Two load-bearing filters are applied:

- Monitor streams are excluded. Plasma keeps several `Stream/Input/Audio` nodes
  in state `running` forever for its volume meters; without the filter the
  daemon reports a call that never ends.
- `/dev/video*` nodes are filtered by `VIDIOC_QUERYCAP`. Otherwise uvcvideo
  metadata nodes, v4l2loopback and hardware video decoders all count as
  webcams, on some machines that would mean watching a video reports "camera on".

## Requirements

- Linux, Python 3.11+
- No Python dependencies
- **Optional:** PipeWire (`pw-dump`, from the `pipewire` package), for microphone
 detection support

Wayland and X11 should both work; nothing here relies on the display server.

### Without PipeWire

The daemon runs camera-only:

```
WARNING microphone detection unavailable (pw-dump not found in PATH); continuing with the camera only -- audio-only calls will not be detected
INFO    watching 2 camera device(s); microphone unavailable, running camera-only; dwell 10.0s
```

Video calls are still detected, with the same latency. You lose is audio-only
calls, and video calls that start with the camera off.

It recovers by itself. PipeWire being installed, started or stopped under a
running daemon is handled by a periodic check, no restart is required.

```
INFO    microphone detection restored after 4 failed attempt(s)
```

`tallyd --check` reports the degradation as `"microphone_available": false`.
If there is also no camera, the daemon logs an error and keeps running, a
webcam may still be plugged in later, which will also be handled correctly.

## Install

### Arch

Available through the [AUR](https://aur.archlinux.org/packages/tallyd).

Or with the package built from source:

```console
$ cd packaging && makepkg -si
```

### From source

```console
$ python -m build --wheel --no-isolation
$ python -m installer --prefix=/usr --destdir=/ dist/*.whl   # needs root
```

For a throwaway user-level install, `pip install --user .` works but will not
install the service; copy `data/tallyd.service` to
`~/.config/systemd/user/` and point `ExecStart=` at the installed script location.

## Configure

```console
$ mkdir -p ~/.config/tallyd
$ cp /usr/share/tallyd/tallyd.conf.example ~/.config/tallyd/tallyd.conf
```

```toml
[detection]
poll_interval = 2.0   # microphone polling interval [s]
dwell = 10.0          # hold "in call" state for this long after after capture stops [s]

[actions]
on_call_start = "notify-send 'Call detected' \"$TALLYD_SOURCES\""
on_call_end   = "notify-send 'Call ended'"
```

Actions get `TALLYD_STATE` (`active`/`inactive`) and `TALLYD_SOURCES`
(what triggered it). They are spawned, never waited on, so a slow or hung
action cannot stall detection.

`dwell` is worth tuning. Teams, Zoom and Discord mute in software and
keep the stream running, but browser WebRTC may drop the audio track
on mute, without a dwell, muting yourself would look like the call ending.

## Run

```console
$ systemctl --user enable --now tallyd
$ journalctl --user -fu tallyd
```

The unit is `PartOf=graphical-session.target`, so it follows your desktop
session in and out. If your setup does not start `graphical-session.target`,
install it under `default.target` instead:

```console
$ systemctl --user add-wants default.target tallyd.service
```

The daemon needs no privileges beyond the user's, and the unit is intentionally
left almost unsandboxed, its whole job is to run your commands so a sandbox
around it would be a sandbox around those too. `PrivateDevices=` in particular
would hide `/dev/video*` and break detection outright.

## What it will not detect

1. **A listen-only call**: dial-in, or a webinar you only watch. No capture,
   no signal. Out of reach without the platform APIs this exists to avoid.
2. **A camera held open without streaming.** `inotify` sees the `open(2)`, not
   `VIDIOC_STREAMON`. Fine as a privacy trigger, imprecise as a usage meter.
3. **Capture by another user or by root.** The `/proc` scan only sees your own
   processes. Normal for a desktop call.
4. **Screen sharing**, which is a separate signal
   (`org.freedesktop.portal.ScreenCast` sessions on D-Bus) and is not wired up.

It *will* fire for any microphone use: voice typing, a recorder, OBS. For a
privacy trigger that is arguably the correct behaviour, the microphone is live either way.

## Development

```console
$ python tests/test_tallyd.py     # no framework needed; pytest also works
$ PYTHONPATH=src python -m tallyd --check -v
```
