"""Checks for the logic that is not obviously correct by reading it.

Runnable two ways, no framework required:
    python tests/test_tallyd.py
    pytest tests/
"""

import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The degraded-PipeWire tests deliberately trip warnings; keep them out of the
# test output so a failure is the only thing that stands out.
logging.getLogger("tallyd.mic").setLevel(logging.CRITICAL)

from tallyd.camera import V4L2_CAP_DEVICE_CAPS, V4L2_CAP_VIDEO_CAPTURE, capture_caps
from tallyd.config import ConfigError, from_mapping
from tallyd.daemon import END, START, CallState
from tallyd.mic import MicWatcher, active_streams


def _node(node_id, state, media_class="Stream/Input/Audio", **props):
    return {"id": node_id, "type": "PipeWire:Interface:Node",
            "info": {"state": state, "props": {"media.class": media_class, **props}}}


def test_mic_detects_a_real_capture_stream():
    dump = [_node(134, "running", **{"application.process.binary": "firefox"})]
    assert active_streams(dump) == ["mic:firefox"]


def test_mic_ignores_monitor_streams():
    """plasmashell keeps peak-meter monitors in state "running" permanently;
    without this filter the daemon reports a call that never ends."""
    dump = [
        _node(107, "running", **{"application.process.binary": "plasmashell",
                                 "stream.monitor": "true"}),
        _node(264, "running", **{"application.process.binary": "plasmashell",
                                 "stream.monitor": True}),  # pw-dump emits both forms
    ]
    assert active_streams(dump) == []


def test_mic_ignores_idle_streams_and_the_bluez_internal_node():
    dump = [
        _node(134, "suspended", **{"application.process.binary": "firefox"}),
        _node(265, "running", media_class="Stream/Input/Audio/Internal"),
        _node(70, "running", media_class="Audio/Source"),
    ]
    assert active_streams(dump) == []


def test_mic_survives_junk_in_the_dump():
    assert active_streams([None, {}, {"type": "PipeWire:Interface:Core"},
                           _node(1, "running")]) == ["mic:node-1"]


# -- PipeWire is optional ------------------------------------------------
# `echo` stands in for a working pw-dump: it exists on PATH and prints JSON.
_WORKING = ("echo", "[]")
_MISSING = ("pw-dump-does-not-exist",)


def test_missing_pipewire_degrades_rather_than_failing():
    """No PipeWire must leave the camera half running, not raise."""
    watcher = MicWatcher(command=_MISSING)
    assert watcher.active(now=0.0) == []
    assert watcher.available is False


def test_degraded_watcher_backs_off_instead_of_retrying_every_tick():
    """A missing pw-dump will not reappear in two seconds; retrying at
    poll_interval would fork a doomed subprocess on every single tick."""
    watcher = MicWatcher(command=_MISSING, retry_interval=30.0)
    for tick in (0.0, 2.0, 4.0):
        watcher.active(now=tick)
    assert watcher.failures == 3                 # probed, then backed off
    watcher.active(now=6.0)
    watcher.active(now=20.0)
    assert watcher.failures == 3                 # suppressed inside the window
    watcher.active(now=40.0)
    assert watcher.failures == 4                 # window elapsed, probed again


def test_backoff_threshold_is_configurable():
    watcher = MicWatcher(command=_MISSING, retry_interval=30.0,
                         failures_before_backoff=1)
    watcher.active(now=0.0)
    watcher.active(now=2.0)
    assert watcher.failures == 1                 # backed off after the first miss


def test_microphone_recovers_without_a_restart():
    """PipeWire can be installed or started under an already-running daemon."""
    watcher = MicWatcher(command=_MISSING)
    watcher.active(now=0.0)
    assert watcher.available is False

    watcher._command = _WORKING  # PipeWire appears
    assert watcher.active(now=1.0) == []
    assert watcher.available is True
    assert watcher.failures == 0


def test_a_broken_pipewire_degrades_the_same_way_as_a_missing_one():
    """pw-dump present but the daemon down, or returning junk, is the same
    situation from here: no microphone signal, camera unaffected."""
    watcher = MicWatcher(command=("echo", "not json"))
    assert watcher.active(now=0.0) == []
    assert watcher.available is False


def _capability(capabilities, device_caps):
    buf = bytearray(104)
    struct.pack_into("=II", buf, 84, capabilities, device_caps)
    return bytes(buf)


def test_capture_caps_prefers_device_caps():
    """A uvcvideo metadata node advertises VIDEO_CAPTURE in `capabilities`
    (which describes the whole physical device) but not in `device_caps`."""
    assert capture_caps(_capability(V4L2_CAP_DEVICE_CAPS | V4L2_CAP_VIDEO_CAPTURE, 0)) is False
    assert capture_caps(_capability(V4L2_CAP_DEVICE_CAPS, V4L2_CAP_VIDEO_CAPTURE)) is True


def test_capture_caps_falls_back_when_device_caps_unsupported():
    assert capture_caps(_capability(V4L2_CAP_VIDEO_CAPTURE, 0)) is True
    assert capture_caps(_capability(0x2, 0)) is False  # VIDEO_OUTPUT only


def test_call_state_fires_each_edge_exactly_once():
    state = CallState(dwell=10.0)
    assert state.update(["mic:zoom"], 100.0) == START
    assert state.update(["mic:zoom"], 101.0) is None
    assert state.update([], 105.0) is None          # inside the dwell window
    assert state.update([], 110.9) is None          # dwell runs from 101.0
    assert state.update([], 111.0) == END
    assert state.update([], 112.0) is None


def test_dwell_absorbs_a_mid_meeting_mute():
    """Browser WebRTC can genuinely drop the audio track on mute; the dwell is
    what stops that from looking like the call ending."""
    state = CallState(dwell=10.0)
    state.update(["mic:firefox"], 0.0)
    assert state.update([], 5.0) is None
    assert state.update(["mic:firefox"], 8.0) is None  # unmuted, still one call
    assert state.in_call
    assert state.update([], 17.9) is None
    assert state.update([], 18.0) == END   # 10s after the last signal, at t=8


def test_zero_dwell_ends_immediately_without_spinning():
    """deadline() must stay None while capture is live, or the event loop polls
    with a 0 ms timeout and burns a core."""
    state = CallState(dwell=0.0)
    state.update(["camera:/dev/video0"], 0.0)
    assert state.deadline() is None
    assert state.update([], 0.0) == END


def test_deadline_tracks_the_dwell_window():
    state = CallState(dwell=10.0)
    state.update(["mic:zoom"], 100.0)
    assert state.deadline() is None      # still capturing, nothing to wait for
    state.update([], 102.0)
    assert state.deadline() == 110.0     # counting down from the last signal


def test_config_defaults_and_validation():
    assert from_mapping({}).dwell == 10.0
    assert from_mapping({"detection": {"dwell": 0}}).dwell == 0.0
    assert from_mapping({"actions": {"on_call_start": "x"}}).on_call_start == "x"
    assert from_mapping({}).mic_retry_interval == 30.0
    assert from_mapping({}).mic_failures_before_backoff == 3
    mic = from_mapping({"detection": {"mic_retry_interval": 5,
                                      "mic_failures_before_backoff": 1}})
    assert (mic.mic_retry_interval, mic.mic_failures_before_backoff) == (5.0, 1)
    for bad in ({"detection": {"poll_interval": 0}},
                {"detection": {"mic_retry_interval": -1}},
                {"detection": {"mic_failures_before_backoff": 0}},
                {"detection": {"mic_failures_before_backoff": 2.5}},
                {"detection": {"dwell": -1}},
                {"detection": {"dwell": "soon"}},
                {"detection": {"dwell": True}},
                {"actions": {"on_call_end": ["x"]}},
                {"actions": "nope"}):
        try:
            from_mapping(bad)
        except ConfigError:
            continue
        raise AssertionError(f"accepted invalid config: {bad}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
