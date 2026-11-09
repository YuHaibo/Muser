import asyncio
import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading

import pytest
from muser.transcripts import Transcripts
from muser.audio import Audio, MicShaper, Resampler
from muser.worker import App, AppService, validate_prefs, DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


def test_transcript_final_replaces_deltas_and_deduplicates():
    events = []
    t = Transcripts(events.append)
    t.feed(dict(type="response.created", response={"id": "r1"}))
    piece = dict(type="response.output_audio_transcript.delta", response_id="r1", delta="你", event_id="e1")
    t.feed(piece)
    t.feed(piece)
    t.feed(dict(type="response.output_audio_transcript.delta", response_id="r1", delta="好", event_id="e2"))
    t.feed(dict(type="response.output_audio_transcript.done", response_id="r1", transcript="你好！"))
    t.feed(dict(type="response.output_audio_transcript.delta", response_id="r1", delta="late"))
    assert [x["text"] for x in events] == ["你", "你好", "你好！"]
    assert events[-1]["final"]


def test_interruption_fences_late_text_until_matching_new_turn():
    events = []
    t = Transcripts(events.append)
    t.feed(dict(type="response.created", response={"id": "old"}))
    t.feed(dict(type="response.output_audio_transcript.delta", delta="old"))
    t.interrupt()
    t.feed(dict(type="response.output_audio_transcript.done", response_id="old", transcript="late"))
    t.feed(dict(type="input_audio_buffer.speech_started", item_id="u2"))
    t.feed(dict(type="hatch.voice.turn_started", item_id="u1", submission_message_id="stale"))
    t.feed(dict(type="response.output_audio_transcript.delta", response_id="new", delta="wrong"))
    assert len(events) == 1
    t.feed(dict(type="hatch.voice.turn_started", item_id="u2", submission_message_id="fresh"))
    t.feed(dict(type="response.output_audio_transcript.done", response_id="old", transcript="still stale"))
    t.feed(dict(type="response.output_audio_transcript.delta", response_id="new", delta="new"))
    assert events[-1]["text"] == "new"
    assert len(events) == 2


def test_user_final_and_controls_are_safe():
    events = []
    t = Transcripts(events.append)
    t.feed(dict(type="conversation.item.input_audio_transcription.completed", item_id="u", transcript="Hello\x1b[31m\n世界"))
    assert events[0]["role"] == "user"
    assert "\x1b" not in events[0]["text"]
    assert events[0]["final"]


def test_streaming_resampler_retains_boundaries():
    pcm = struct.pack("<" + "h" * 1000, *range(1000))
    whole = Resampler(24000, 48000).feed(pcm)
    split = Resampler(24000, 48000)
    chunks = b"".join(split.feed(pcm[i:i+74]) for i in range(0,len(pcm),74))
    assert chunks == whole
    assert 1996 <= len(chunks) // 2 <= 2000


def make_audio():
    a = object.__new__(Audio)
    a.capture = queue.Queue(maxsize=8)
    a.tail = bytearray(b"stale")
    a.muted = False
    a.input_channels = 1
    a.playback = __import__("collections").deque()
    a.playback_bytes = 0
    a.lock = threading.Lock()
    a.prefs = {"volume": 50}
    a.mic = MicShaper()
    a.failure = False
    return a


@pytest.mark.parametrize("capture_rate", [24000, 48000])
def test_cardputer_capture_selects_the_active_stereo_channel(tmp_path, monkeypatch, capture_rate):
    from muser import audio

    card = tmp_path / "id"
    card.touch()
    monkeypatch.setattr(audio, "CARDPUTER_AUDIO", card)

    class Stream:
        def __init__(self, **settings):
            self.settings = settings

        def start(self):
            pass

        abort = close = start

    class Sound:
        PortAudioError = OSError
        RawInputStream = RawOutputStream = Stream

        def query_devices(self, device, kind):
            return {"default_samplerate": 48000}

        def check_input_settings(self, **kwargs):
            if kwargs["samplerate"] < capture_rate:
                raise OSError("rate unavailable")

        def check_output_settings(self, **kwargs):
            pass

    adapter = Audio(DEFAULTS.copy(), sound=Sound())
    try:
        assert adapter.input.settings["channels"] == 2
        assert adapter.input.settings["samplerate"] == capture_rate
        assert adapter.output.settings["samplerate"] == 24000
        frames = capture_rate // 25
        stereo = struct.pack("<hh", 1200, 0) * frames
        adapter._capture(stereo, frames, None, False)
        expected = struct.pack("<h", 1200) * frames
        if capture_rate == 48000:
            expected = audio.Pcm48To24().feed(expected)
        expected = audio.MicShaper().feed(expected)
        assert adapter.capture.get_nowait() == expected
    finally:
        adapter.close()


def test_mute_discards_queued_microphone_content():
    a = make_audio()
    a.capture.put(b"old mic")
    a.set_muted(True)
    assert a.capture.empty() and not a.tail
    a.down = Resampler(24000,24000)
    a._capture(struct.pack("<hh",5000,10000),2,None,False)
    assert a.capture.get_nowait() == bytes(4)
    assert a.mic_level == 0


def test_mic_shaper_lifts_speech_band_and_rejects_dc():
    frames = 960
    tone = struct.pack("<" + "h" * frames,
        *[round(1000 * math.sin(2 * math.pi * 1000 * i / 24000)) for i in range(frames)])
    out = MicShaper().feed(tone)
    values = struct.unpack("<" + "h" * (len(out) // 2), out)
    rms_out = math.sqrt(sum(value * value for value in values) / len(values))
    assert 2700 <= rms_out <= 3000

    shaper = MicShaper()
    dc = struct.pack("<h", 5000) * frames
    for _ in range(120):
        out = shaper.feed(dc)
    tail = struct.unpack("<" + "h" * (len(out) // 2), out)
    assert max(abs(value) for value in tail) < 200


def test_mic_shaper_limits_loud_input_instead_of_clipping():
    frames = 960
    loud = struct.pack("<" + "h" * frames,
        *[round(20000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(frames)])
    out = MicShaper().feed(loud)
    values = struct.unpack("<" + "h" * (len(out) // 2), out)
    assert max(abs(value) for value in values) <= 32767
    assert max(abs(value) for value in values) >= 29000


def test_playback_joins_fragments_and_applies_gain():
    a = make_audio()
    a.playback.extend([struct.pack("<h",1000),struct.pack("<hh",2000,3000)])
    a.playback_bytes = 6
    output = bytearray(8)
    a._play(output,4,None,False)
    assert struct.unpack("<hhhh",output) == (500,1000,1500,0)
    assert a.playback_bytes == 0
    assert a.speaker_level > 0


def test_temporary_audio_callback_flags_do_not_terminate_a_live_call():
    a=make_audio()
    a.down=Resampler(24000,24000)
    a._capture(bytes(1920),960,None,True)
    out=bytearray(1920)
    a._play(out,960,None,True)
    assert not a.failure
    assert a.callback_warnings==2
    assert a.capture.qsize()==1


def test_audio_close_releases_both_streams_when_one_abort_fails():
    a=make_audio()
    calls=[]
    class Stream:
        def __init__(self,name,broken=False):self.name=name;self.broken=broken
        def abort(self):
            calls.append((self.name,"abort"))
            if self.broken:raise OSError("synthetic stopped stream")
        def close(self):calls.append((self.name,"close"))
    a.streams=[Stream("input"),Stream("output",True)]
    a.capture.put(b"private microphone buffer")
    with pytest.raises(OSError):a.close()
    assert ("input","close") in calls and ("output","close") in calls
    assert a.streams==[] and a.capture.empty() and not a.tail
    before=list(calls);a.close();assert calls==before


def test_no_remote_machine_commands_registered():
    class Identity:
        node_id = "synthetic-device"
    s = AppService(Identity(),None)
    assert s.describe().commands == {}
    assert not s.run_command("system.run",{})["ok"]


def test_preferences_validation():
    for change in ({"volume":101},{"overall_timeout":0},{"input_device":-1},{"volume":True}):
        with pytest.raises(ValueError):
            validate_prefs({**DEFAULTS, **change})


def test_demo_chat_compose_clear_shutdown_without_files(tmp_path,monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR", str(tmp_path / "no-state"))
    events=[]
    async def scenario():
        app=App(events.append,demo=True)
        await app.command({"command":"call.start"})
        await app.command({"command":"compose"})
        assert any(e["type"]=="compose.ready" for e in events)
        await app.command({"command":"chat.send","text":"hello"})
        await app.chat_task
        assert len([e for e in events if e["type"]=="transcript"]) == 2
        await app.command({"command":"clear"})
        assert events[-1]["type"] == "cleared"
        await app.command({"command":"shutdown"})
        await app.shutdown()
    asyncio.run(scenario())
    assert not (tmp_path / "no-state").exists()


def test_json_process_protocol_handles_invalid_input_and_eof(tmp_path):
    env = {**os.environ,"MUSER_STATE_DIR":str(tmp_path / "none")}
    proc=subprocess.run([sys.executable,str(ROOT/"backend/worker.py"),"--demo"],
        input='not json\n[]\n{"command":"mic.mute","muted":true}\n{"command":"shutdown"}\n',
        text=True,capture_output=True,timeout=8,env=env)
    assert proc.returncode == 0, proc.stderr
    records=[json.loads(line) for line in proc.stdout.splitlines()]
    assert records[0]["app"] == "Muser"
    assert sum(r["type"]=="error" for r in records) == 2
    assert any(r["type"]=="muted" and r["value"] for r in records)
    assert records[-1]["type"] == "stopped"
    assert not (tmp_path/"none").exists()


def test_state_and_preferences_permissions(tmp_path,monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR", str(tmp_path / "private"))
    from muser.worker import initialize_state
    from musegadget import config
    path = initialize_state()
    config.save_json("preferences.json", DEFAULTS)
    assert path.stat().st_mode & 0o777 == 0o700
    assert (path/"preferences.json").stat().st_mode & 0o777 == 0o600


def test_audio_events_do_not_exhaust_transcript_identity_budget():
    t=Transcripts(lambda e: None)
    for i in range(10000):
        t.feed(dict(type="continuous_audio_video.delta",event_id=str(i)))
    assert not t.seen


def test_volume_updates_active_audio_preferences_in_place():
    async def scenario():
        app=App(lambda e: None,demo=True)
        active=app.prefs
        await app.settings({"volume":25})
        assert active is app.prefs and active["volume"]==25
    asyncio.run(scenario())


def test_voice_rejects_vm_fallback_and_clears_active_call(tmp_path,monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"private"))
    from musegadget import config,muse_api
    events=[]
    async def scenario():
        app=App(events.append)
        config.save_json(config.PAIRING_FILE,{"access_token":"synthetic"})
        monkeypatch.setattr(muse_api,"fetch_vms_with_status",lambda *a: ([{
            "vm_id":"other-default","vm_auth_token":"synthetic","is_default":True}],200))
        app.session=object()
        app.vm={"vm_id":"exact-target","vm_auth_token":"expired"}
        await app.start_call()
        await app.call_task
        assert any(e["type"]=="error" for e in events)
        assert any(e["type"]=="call" and e["active"] for e in events)
        assert any(e["type"]=="call" and not e["active"] for e in events)
        assert app.call is None and app.audio is None
        await app.shutdown()
    asyncio.run(scenario())


def test_language_defaults_to_english_and_switches_all_app_messages():
    async def scenario():
        events=[]
        app=App(events.append,demo=True)
        assert app.prefs["language"]=="en"
        await app.connect()
        assert events[-1]["text"]=="Demo mode · Not connected"
        await app.settings({"language":"zh-CN"})
        await app.connect()
        assert events[-1]["text"]=="演示模式 · 未连接服务"
        app.error("test","未知操作")
        assert events[-1]["message"]=="未知操作"
        await app.settings({"language":"en"})
        app.error("test","未知操作")
        assert events[-1]["message"]=="Unknown operation."
        with pytest.raises(ValueError):
            await app.settings({"language":"xx"})
    asyncio.run(scenario())


def test_remote_unpair_updates_app_state_without_reopening_microphone():
    from musegadget.link_client import Outcome
    async def scenario():
        events=[]
        app=App(events.append,demo=True)
        class Session:
            registered=asyncio.Event()
            async def run(self,stop):
                self.registered.set()
                await asyncio.sleep(.01)
                return Outcome.UNPAIRED
        service=AppService(None,None)
        service.app=app
        assert await service.serve(Session(),{},asyncio.Event()) is Outcome.UNPAIRED
        assert app.session is None and app.audio is None
        assert events[-1]["mode"]=="unpaired"
    asyncio.run(scenario())
