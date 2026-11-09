import asyncio
import json
import time

import pytest
from musegadget import config, identity, muse_api
from muser.worker import App, AppService


@pytest.fixture
def paired_app(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR", str(tmp_path / "state"))
    events=[]
    app=App(events.append)
    config.save_json(config.PAIRING_FILE, {
        "access_token":"synthetic-private-access",
        "refresh_token":"synthetic-private-refresh",
        "token_type":"device",
        "access_token_saved_at":int(time.time()),
    })
    return app,events


@pytest.mark.parametrize("http_status,stage",[(None,"network_error"),(503,"http_error"),(403,"http_error"),(200,"no_muse")])
def test_cloud_lookup_failure_keeps_pairing_and_reports_service_retry(paired_app,monkeypatch,http_status,stage):
    app,events=paired_app
    before=(config.state_dir()/config.PAIRING_FILE).read_bytes()
    monkeypatch.setattr(muse_api,"fetch_vms_with_status",lambda *args:([],http_status))
    async def scenario():
        service=AppService(identity.load_or_create(),None)
        service.app=app
        async def stop_after_retry(delay):
            service.stop()
        service._sleep=stop_after_retry
        await service.run()
    asyncio.run(scenario())
    assert (config.state_dir()/config.PAIRING_FILE).read_bytes()==before
    assert any(e["type"]=="connection.detail" and e["stage"]==stage and e["paired"] for e in events)
    assert any(e["type"]=="status" and e["mode"]=="offline" for e in events)
    assert not any(e["type"]=="status" and e["mode"]=="unpaired" for e in events)
    assert app.paired
    diagnostic=(config.state_dir()/"connection-status.json").read_text()
    assert "synthetic-private" not in diagnostic
    assert (config.state_dir()/"connection-status.json").stat().st_mode&0o777==0o600


def test_authoritative_refresh_revocation_can_require_pairing(paired_app,monkeypatch):
    app,events=paired_app
    monkeypatch.setattr(muse_api,"fetch_vms_with_status",lambda *args:([],401))
    monkeypatch.setattr(muse_api,"refresh_device_token",lambda *args:(None,401))
    async def scenario():
        service=AppService(identity.load_or_create(),None)
        service.app=app
        async def stop_after_retry(delay):
            service.stop()
        service._sleep=stop_after_retry
        await service.run()
    asyncio.run(scenario())
    assert not (config.state_dir()/config.PAIRING_FILE).exists()
    assert not app.paired
    assert any(e["type"]=="status" and e["mode"]=="unpaired" for e in events)


def test_saved_pairing_is_published_before_first_cloud_lookup(paired_app,monkeypatch):
    app,events=paired_app
    async def idle_service(service):
        return
    monkeypatch.setattr(AppService,"run",idle_service)
    async def scenario():
        await app.connect()
        assert events[0]==dict(type="pairing.state",paired=True)
        assert events[-1]["mode"]=="connecting"
        await app.service_task
        await app.start_call()
        assert events[-1]["type"]=="error" and "Already paired" in events[-1]["message"]
        assert config.load_json(config.PAIRING_FILE)
        await app.shutdown()
    asyncio.run(scenario())


def test_pair_success_emits_cloud_connecting_without_waiting_for_network(paired_app,monkeypatch):
    app,events=paired_app
    class Process:
        returncode=0
        @property
        def stdout(self):
            async def lines():
                yield b'{"type":"pairing","name":"MuseGadgetSYNTHETIC","timeout":600}\n'
                yield b'{"type":"pair.result","outcome":"paired"}\n'
            return lines()
        async def wait(self):
            return 0
    async def create(*args,**kwargs):
        return Process()
    monkeypatch.setattr(asyncio,"create_subprocess_exec",create)
    connected=[]
    async def connect():
        connected.append(True)
    app.connect=connect
    asyncio.run(app._pair())
    assert connected==[True] and app.paired
    result_index=next(i for i,e in enumerate(events) if e["type"]=="pair.result")
    assert any(e["type"]=="status" and e["mode"]=="connecting" for e in events[result_index+1:])


def test_connection_diagnostic_never_serializes_arbitrary_error_contents(paired_app):
    app,events=paired_app
    app.connection_state("http_error","synthetic-private-secret")
    recorded=(config.state_dir()/"connection-status.json").read_text()
    assert "synthetic-private-secret" not in recorded
    assert json.loads(recorded)["http_status"] is None
