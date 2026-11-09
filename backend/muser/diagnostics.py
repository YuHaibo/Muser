"""Private, bounded metadata logs; never use SDK/network log messages here."""
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import time


class _PrivateHandler(RotatingFileHandler):
    def _open(self):
        flags=os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,"O_NOFOLLOW",0)
        fd=os.open(self.baseFilename,flags,0o600)
        os.fchmod(fd,0o600)
        return os.fdopen(fd,"a",encoding="utf-8")


class Diagnostics:
    EVENTS={"app.start","app.stop","connection.state","chat.state","voice.command","voice.state","voice.protocol","voice.stats","voice.end","voice.cleanup","voice.error"}
    NUMERIC={"call_seq","http_status","elapsed_seconds","input_device","output_device","input_rate","output_rate","input_channels","input_blocks","input_drops","output_blocks","input_sent","input_voiced","input_gated_drops","audio_received_samples","audio_played_samples","audio_dropped_samples","advisory_errors","mic_level","mic_peak","speaker_level","speaker_peak","capture_queue","playback_ms","sdk_playback_ms","active_turns","callback_warnings"}
    BOOLEAN={"paired","muted","ready","server_hold","mic_gated","gate_waiting","gate_blocked","gate_confirmed","gate_fenced","input_active","output_active","input_default","output_default","end_call_acknowledged"}
    ENUM={
        "state":{"idle","starting","live","stopping"},
        "outcome":{"closed","auth_rejected","forbidden","unpaired","stopped"},
        "action":{"start","start_ignored","stop","interrupt","mute"},
        "reason":{"cancelled","idle_timeout","overall_timeout","remote_end","remote_stop","failed","unknown","app_start"},
        "stage":{"idle","vm_lookup","transport","audio_open","audio_close","transport_close","cleanup_complete","admission","live","ended","failed","waiting_reply","complete","reply_missing","unpaired","refresh_wait","refreshing","network_error","no_muse","http_error","connecting","connected","disconnected"},
        "language":{"en","zh-CN"},
        "protocol":{"hatch.voice.session","input_audio_buffer.speech_started","input_audio_buffer.speech_stopped","hatch.voice.turn_started","hatch.voice.turn_settled","hatch.voice.switch_input_hold","hatch.voice.input_suppressed","conversation.item.input_audio_transcription.completed","response.created","response.done","response.completed","response.output_audio_transcript.done","error"},
    }

    def __init__(self,state_dir,max_bytes=256*1024,backups=3):
        folder=Path(state_dir)/"logs"
        if folder.is_symlink():raise OSError("log directory is a symlink")
        folder.mkdir(mode=0o700,parents=True,exist_ok=True);folder.chmod(0o700)
        self.handler=_PrivateHandler(folder/"muser.log",maxBytes=max_bytes,backupCount=backups,delay=True,encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def record(self,event,**fields):
        if event not in self.EVENTS or self.handler is None:return
        entry={"time":time.strftime("%Y-%m-%dT%H:%M:%S%z"),"event":event}
        for key,value in fields.items():
            if isinstance(value,str) and ("mgst_" in value or value.lower().startswith("bearer ")):continue
            if key in self.NUMERIC and type(value) in (int,float) and math.isfinite(value):entry[key]=round(value,4) if type(value) is float else value
            elif key in self.BOOLEAN and type(value) is bool:entry[key]=value
            elif key in self.ENUM and isinstance(value,str) and value in self.ENUM[key]:entry[key]=value
            elif key=="error_type" and isinstance(value,str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",value):entry[key]=value
            elif key=="version" and isinstance(value,str) and re.fullmatch(r"\d+\.\d+\.\d+",value):entry[key]=value
        record=logging.LogRecord("muser.metadata",logging.INFO,"",0,json.dumps(entry,separators=(",",":")),(),None)
        try:self.handler.handle(record)
        except (OSError,ValueError):pass

    def close(self):
        if self.handler is not None:self.handler.close();self.handler=None
