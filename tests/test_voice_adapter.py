"""Exercise Muser's observer through the SDK's real in-process Noise transport."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).parent / "upstream"))
from test_streaming import connected, chunk
from test_voice_call import VoiceCall, Output, source, TONE, admit, next_event, hangup
from muser.transcripts import Transcripts


def test_live_upload_keeps_capture_cadence_with_send_latency(monkeypatch):
    from musegadget import voice_call

    class EndOfProbe(Exception):
        pass

    async def scenario():
        call = VoiceCall(None, device_id="synthetic")
        call.ready.set()
        now = 0.0
        sent = []

        async def sleep(delay):
            nonlocal now
            now += delay

        async def send(event):
            nonlocal now
            sent.append(now)
            now += .006  # Encryption and socket write take part of each 40 ms block.

        async def microphone():
            nonlocal now
            for _ in range(4):
                yield TONE
            now = .5  # A pause in capture must not cause a burst on resume.
            yield TONE
            yield TONE
            raise EndOfProbe

        monkeypatch.setattr(voice_call, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: now))
        monkeypatch.setattr(voice_call, "asyncio", SimpleNamespace(sleep=sleep))
        monkeypatch.setattr(call, "_event", send)
        with pytest.raises(EndOfProbe):
            await call._input(microphone(), realtime=True)
        assert sent == pytest.approx([0, .04, .08, .12, .5, .54])

    asyncio.run(scenario())


@pytest.mark.parametrize("broken_observer", [False,True])
def test_voice_observer_keeps_admission_and_explicit_hangup(broken_observer):
    output=[]
    async def scenario():
        async with connected(register_device=False) as (session,vm,urls):
            assembler=Transcripts(output.append)
            def observer(event):
                assembler.feed(event)
                if broken_observer and event.get("type")=="response.output_audio_transcript.done":
                    raise RuntimeError("synthetic observer failure")
            call=VoiceCall(session,device_id="homelink-synthetic",half_duplex=False,on_event=observer)
            running=asyncio.create_task(call.run(source(bytes(len(TONE))),Output()))
            events=[dict(type="conversation.item.input_audio_transcription.completed",item_id="u1",transcript="你好"),
                    dict(type="response.created",response={"id":"r1"}),
                    dict(type="response.output_audio_transcript.delta",response_id="r1",delta="Hello"),
                    dict(type="response.output_audio_transcript.done",response_id="r1",transcript="Hello!")]
            try:
                media,start=await admit(vm,extra=events)
                if not broken_observer:
                    assert (await next_event(vm))["type"]=="input_audio_buffer.append"
                    await chunk(vm,media,end=True)
                await hangup(vm,media,start,media_ended=not broken_observer)
                if broken_observer:
                    with pytest.raises(RuntimeError,match="synthetic observer"):
                        await asyncio.wait_for(running,2)
                else:
                    assert await asyncio.wait_for(running,2)=="remote_end"
                assert call.end_call_acknowledged
                if not broken_observer:
                    assert call.input_sent>=1 and call.input_voiced==0
                assert not session._requests
            finally:
                running.cancel()
                await asyncio.gather(running,return_exceptions=True)
    asyncio.run(scenario())
    assert [e["text"] for e in output]==["你好","Hello","Hello!"]
