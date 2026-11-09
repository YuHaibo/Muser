import asyncio
import http.client
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from musegadget import config
from muser.phone_setup import PhoneSetup
from muser.worker import App, initialize_state

TOKEN = "mgst_" + "A" * 42 + "w"  # Synthetic SDK fixture, never a real credential.


def request(portal, method="GET", data=None, path=None, headers=None):
    connection = http.client.HTTPConnection(portal.host, portal.server.server_port, timeout=3)
    try:
        extra = headers or {}
        body = None if data is None else urlencode(data)
        if body is not None:
            extra = {"Content-Type":"application/x-www-form-urlencoded", **extra}
        connection.request(method, path or portal.path, body=body, headers=extra)
        reply = connection.getresponse()
        return reply.status, dict(reply.headers), reply.read().decode()
    finally:
        connection.close()


def form_data(portal, token=TOKEN):
    code, _, body = request(portal,path="/")
    assert code == 200
    assert '<form method="post" action="/">' in body
    assert 'name="form_token"' not in body
    return {"token": token}


def assert_listener_closed(portal):
    assert portal.closed and not portal.thread.is_alive()
    assert portal.server.fileno() == -1
    with pytest.raises(OSError):
        socket.create_connection((portal.host, portal.server.server_port), timeout=.3)


@pytest.fixture
def state(tmp_path,monkeypatch):
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"muser"))
    monkeypatch.delenv(config.SDK_TOKEN_ENV, raising=False)
    return initialize_state()


def test_phone_form_one_use_private_storage_and_no_token_reflection(state):
    async def scenario():
        portal=PhoneSetup(host="127.0.0.1")
        try:
            code,headers,body=request(portal)
            assert code==200 and "Import &amp; pair" in body
            assert headers["Cache-Control"]=="no-store"
            assert headers["Referrer-Policy"]=="same-origin"
            assert TOKEN not in portal.url and TOKEN not in body
            assert portal.path=="/" and portal.url=="http://"+portal.authority+"/"
            assert request(portal,path="/",headers={"Host":"CardputerZero.local:8050"})[0]==200
            assert not (state/"sdk_token").exists()
            assert request(portal,"POST",form_data(portal,"invalid"))[0]==400
            code,_,body=request(portal,"POST",form_data(portal))
            assert code==200 and TOKEN not in body
            assert await portal.wait()
            assert config.sdk_token()==TOKEN
            assert (state/"sdk_token").stat().st_mode&0o777==0o600
            assert_listener_closed(portal)
        finally:
            await portal.close()
        assert portal.closed and not portal.thread.is_alive()
    asyncio.run(scenario())


def test_expired_link_rejects_import_and_closes(state):
    async def scenario():
        portal=PhoneSetup(host="127.0.0.1",lifetime=-1)
        try:
            assert request(portal,"POST",{"token":TOKEN})[0]==404
            assert not await portal.wait()
            assert_listener_closed(portal)
            assert not (state/"sdk_token").exists()
        finally:
            await portal.close()
    asyncio.run(scenario())


def test_in_app_import_automatically_starts_bluetooth_pairing(state,monkeypatch):
    import muser.worker as worker
    events=[]
    monkeypatch.setattr(worker,"PhoneSetup",lambda language:PhoneSetup(language,host="127.0.0.1"))
    monkeypatch.setattr(worker.sys,"platform","linux")
    async def scenario():
        app=App(events.append)
        started=[]
        async def paired():
            assert_listener_closed(portal)
            started.append(True)
        app._pair=paired
        try:
            await app.pair()
            setup=next(e for e in events if e["type"]=="setup")
            assert setup["url"]==app.portal.url
            portal=app.portal
            assert request(portal,"POST",form_data(portal))[0]==200
            await app.setup_task
            await app.pair_task
            assert started==[True]
            assert portal.closed and app.portal is None
            assert_listener_closed(portal)
            assert not any("sudo muser-setup" in json.dumps(e) for e in events)
        finally:
            await app.shutdown()
    asyncio.run(scenario())


def test_cancel_before_setup_task_starts_closes_listener(state,monkeypatch):
    import muser.worker as worker
    monkeypatch.setattr(worker,"PhoneSetup",lambda language:PhoneSetup(language,host="127.0.0.1"))
    monkeypatch.setattr(worker.sys,"platform","linux")
    async def scenario():
        app=App(lambda e:None)
        await app.pair()
        portal=app.portal
        await app.cancel_pairing()
        assert_listener_closed(portal)
        assert app.portal is None and app.setup_task is None
        await app.shutdown()
    asyncio.run(scenario())


def test_package_has_no_system_configuration_helper():
    root=Path(__file__).parents[1]
    assert not (root/"packaging/postinst").exists()
    assert not (root/"packaging/bluetooth-setup").exists()
    assert "sudo muser-setup" not in (root/"backend/muser/worker.py").read_text()


@pytest.mark.parametrize("origin", [None, "null", "same-origin"])
def test_browser_form_origins_work_without_hidden_proof_and_close(state, origin):
    async def scenario():
        portal=PhoneSetup(host="127.0.0.1")
        try:
            data=form_data(portal)
            assert set(data)=={"token"}
            headers={} if origin is None else {"Origin": "http://"+portal.authority if origin=="same-origin" else origin}
            headers["Sec-Fetch-Site"]="same-origin"
            status,_,body=request(portal,"POST",data,headers=headers)
            assert status==200 and TOKEN not in body
            assert await portal.wait()
            assert_listener_closed(portal)
        finally:
            await portal.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("extra", [None,{"form_token":"legacy-value"},{"form_token":"中文"}])
def test_null_origin_needs_only_the_sdk_token(state,extra):
    async def scenario():
        portal=PhoneSetup(host="127.0.0.1")
        try:
            data={"token":TOKEN}
            if extra:
                data.update(extra)
            assert request(portal,"POST",data,path="/",headers={"Origin":"null"})[0]==200
            assert await portal.wait()
            assert_listener_closed(portal)
            assert config.sdk_token()==TOKEN
        finally:
            await portal.close()
    asyncio.run(scenario())


def test_origin_and_fetch_metadata_do_not_block_manual_import(state):
    async def scenario():
        portal=PhoneSetup(host="127.0.0.1")
        try:
            assert request(portal,"POST",{"token":TOKEN},path="/",headers={"Origin":"http://browser.test","Sec-Fetch-Site":"cross-site","Host":"CardputerZero.local:8050"})[0]==200
            assert await portal.wait()
            assert_listener_closed(portal)
            assert config.sdk_token()==TOKEN
        finally:
            await portal.close()
    asyncio.run(scenario())


def test_app_exit_closes_active_phone_setup_without_storing_token(state,monkeypatch):
    import muser.worker as worker
    monkeypatch.setattr(worker,"PhoneSetup",lambda language:PhoneSetup(language,host="127.0.0.1"))
    monkeypatch.setattr(worker.sys,"platform","linux")
    async def scenario():
        app=App(lambda event:None)
        await app.pair()
        portal=app.portal
        await asyncio.sleep(0)
        await app.shutdown()
        assert_listener_closed(portal)
        assert not (state/"sdk_token").exists()
    asyncio.run(scenario())
