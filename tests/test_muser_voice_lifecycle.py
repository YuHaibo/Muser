"""Muser-specific call ownership and cancellation regressions."""
import asyncio
import json

from musegadget import config,muse_api
from muser.worker import App


def test_new_call_waits_for_old_cleanup_and_is_not_lost(tmp_path,monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"state"))
    async def scenario():
        app=App(lambda event:None)
        app.session=object();app.vm={"vm_id":"synthetic"}
        cleanup=asyncio.Event();started=asyncio.Event()
        async def call(vm):
            started.set()
            try:await asyncio.Future()
            finally:await cleanup.wait()
        app._call=call
        await app.start_call();await started.wait();old=app.call_task
        stopping=asyncio.create_task(app.stop_call());await asyncio.sleep(0)
        assert app.voice_state=="stopping"
        restarting=asyncio.create_task(app.start_call());await asyncio.sleep(0)
        assert not restarting.done()
        cleanup.set();await stopping;await restarting
        assert app.call_task is not old and not app.call_task.done()
        assert app.voice_state=="starting"
        await app.stop_call();await app.shutdown()
    asyncio.run(scenario())


def test_cancelled_call_writes_end_reason_and_releases_audio(tmp_path,monkeypatch):
    import muser.worker as worker
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"state"))
    app=App(lambda event:None)
    config.save_json(config.PAIRING_FILE,{"access_token":"synthetic-private-access","refresh_token":"synthetic-private-refresh"})
    monkeypatch.setattr(muse_api,"fetch_vms_with_status",lambda *args:([{"vm_id":"target","vm_auth_token":"synthetic-private-vm"}],200))
    ready=asyncio.Event();closed=[]
    class Session:
        def __init__(self,**kwargs):pass
        async def run(self,stop):await stop.wait()
        async def wait_transport_ready(self,timeout):pass
    class Audio:
        mic_level=speaker_level=0
        def __init__(self,prefs):pass
        def set_muted(self,value):pass
        async def frames(self):yield bytes(1920)
        def close(self):closed.append(True)
    class Call:
        def __init__(self,session,**kwargs):self.observer=kwargs["on_event"]
        async def run(self,source,output,**kwargs):
            self.observer({"type":"hatch.voice.session"});ready.set();await asyncio.Future()
    monkeypatch.setattr(worker,"LinkSession",Session);monkeypatch.setattr(worker,"Audio",Audio);monkeypatch.setattr(worker,"VoiceCall",Call)
    async def scenario():
        app.session=object();app.vm={"vm_id":"target"}
        await app.start_call();await ready.wait();await app.stop_call()
        record=json.loads((config.state_dir()/"voice-status.json").read_text())
        assert record["stage"]=="ended" and record["reason"]=="cancelled"
        assert app.voice_state=="idle" and app.call_task is None and app.audio is None
        assert closed==[True]
        log=(config.state_dir()/"logs/muser.log").read_text()
        assert '"event":"voice.end"' in log and '"reason":"cancelled"' in log
        assert "synthetic-private" not in log
        await app.shutdown()
    asyncio.run(scenario())


def test_startup_clears_stale_live_diagnostic_without_opening_audio(tmp_path,monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"state"))
    app=App(lambda event:None)
    record=json.loads((config.state_dir()/"voice-status.json").read_text())
    assert record["stage"]=="idle" and record["reason"]=="app_start"
    assert app.audio is None and app.call is None
    asyncio.run(app.shutdown())
