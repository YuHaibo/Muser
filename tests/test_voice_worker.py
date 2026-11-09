import asyncio
import json
from pathlib import Path

import pytest
from musegadget import config,muse_api
from musegadget.streaming import HTTPStatusError
from muser.worker import App


@pytest.mark.parametrize("failure",[None,"audio_open","admission"])
def test_worker_voice_identity_and_stage_diagnostics(tmp_path,monkeypatch,failure):
    import muser.worker as worker
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"private"))
    events=[]
    app=App(events.append)
    config.save_json(config.PAIRING_FILE,{"access_token":"synthetic-private-access","refresh_token":"synthetic-private-refresh"})
    monkeypatch.setattr(muse_api,"fetch_vms_with_status",lambda *args:([{"vm_id":"target","vm_auth_token":"fresh-private-token"}],200))
    seen=[]
    class Session:
        def __init__(self,**kwargs):
            seen.append(kwargs)
        async def run(self,stop):await stop.wait()
        async def wait_transport_ready(self,timeout):return
    class Audio:
        mic_level=speaker_level=0
        def __init__(self,prefs):
            if failure=="audio_open":raise OSError("synthetic-private-error-body")
        def set_muted(self,value):pass
        async def frames(self):
            yield bytes(1920)
        def close(self):pass
    class Call:
        def __init__(self,session,**kwargs):self.observer=kwargs["on_event"]
        async def run(self,source,output,**kwargs):
            if failure=="admission":raise HTTPStatusError(403,b"synthetic-private-response")
            self.observer({"type":"hatch.voice.session"})
            return "remote_end"
    monkeypatch.setattr(worker,"LinkSession",Session)
    monkeypatch.setattr(worker,"Audio",Audio)
    monkeypatch.setattr(worker,"VoiceCall",Call)
    app.session=object()
    asyncio.run(app._call({"vm_id":"target","vm_auth_token":"stale"}))
    assert len(seen)==1
    assert seen[0]["app_id"]=="hatch-web" and seen[0]["register_device"] is False
    assert seen[0]["vm_id"]=="target" and seen[0]["vm_auth_token"]=="fresh-private-token"
    diagnostic=(config.state_dir()/"voice-status.json").read_text()
    assert "synthetic-private" not in diagnostic
    record=json.loads(diagnostic)
    assert record["stage"]==(failure or "ended")
    if failure=="admission":assert record["http_status"]==403
    assert app.call is None and app.audio is None
