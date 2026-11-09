import json
import logging
from pathlib import Path

from muser.diagnostics import Diagnostics


def test_metadata_log_works_with_sdk_logging_disabled_and_discards_secrets(tmp_path):
    disabled=logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        log=Diagnostics(tmp_path)
        secret="mgst_"+"A"*42+"w"
        log.record("voice.stats",call_seq=2,input_sent=10,mic_level=.004,muted=False,text="private dialogue",token=secret,headers="Bearer private-auth",error_type=secret)
        log.record("voice.protocol",protocol="input_audio_buffer.speech_started",call_seq=2)
        log.close()
        text=(tmp_path/"logs/muser.log").read_text()
        assert secret not in text and "private dialogue" not in text and "private-auth" not in text
        rows=[json.loads(line) for line in text.splitlines()]
        assert rows[0]["input_sent"]==10 and rows[0]["mic_level"]==.004
        assert rows[1]["protocol"]=="input_audio_buffer.speech_started"
        assert (tmp_path/"logs").stat().st_mode&0o777==0o700
        assert (tmp_path/"logs/muser.log").stat().st_mode&0o777==0o600
    finally:logging.disable(disabled)


def test_log_rotation_is_bounded_and_private(tmp_path):
    log=Diagnostics(tmp_path,max_bytes=400,backups=2)
    for i in range(50):log.record("voice.stats",call_seq=i,input_sent=i*20,mic_level=.01)
    log.close()
    files=list((tmp_path/"logs").iterdir())
    assert 1<len(files)<=3
    assert all(p.stat().st_mode&0o777==0o600 for p in files)
    assert all(p.stat().st_size<600 for p in files)


def test_unknown_log_events_and_fields_are_not_serialized(tmp_path):
    log=Diagnostics(tmp_path)
    log.record("raw.server.response",body="private data")
    log.record("voice.end",reason="cancelled",server_body="private data",state="not-a-state")
    log.close()
    rows=[json.loads(line) for line in (tmp_path/"logs/muser.log").read_text().splitlines()]
    assert len(rows)==1 and rows[0]["reason"]=="cancelled"
    assert "server_body" not in rows[0] and "state" not in rows[0]
