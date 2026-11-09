"""JSON-lines child process. No shell execution and no conversation persistence."""
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from musegadget import config, identity, muse_api
from musegadget.link_client import DeviceDescription, LinkSession, Outcome
from musegadget.service import Service, send_and_wait
from musegadget.muse_turn import Reply, AgentStatus
from musegadget.voice_call import VoiceCall
from .audio import Audio
from .transcripts import Transcripts
from .i18n import translate
from . import __version__
from .phone_setup import PhoneSetup
from .diagnostics import Diagnostics

DEFAULTS = dict(volume=70, idle_timeout=45, overall_timeout=300,
                input_device=None, output_device=None, language="en")


def initialize_state():
    fallback = Path("/var/lib/muser") if hasattr(os, "geteuid") and os.geteuid() == 0 else (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/share"))) / "muser")
    path = Path(os.environ.get("MUSER_STATE_DIR", os.environ.get(config.STATE_DIR_ENV, str(fallback))))
    if path.is_symlink():
        raise ValueError("state directory must not be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    os.environ[config.STATE_DIR_ENV] = str(path)
    return path


class AppService(Service):
    def on_connection_state(self, stage, http_status=None):
        self.app.connection_state(stage, http_status)

    def describe(self):
        return DeviceDescription(self.identity.node_id, "Muser", __version__, {})

    def run_command(self, *args, **kwargs):
        return {"ok": False, "error": "unsupported command"}

    async def serve(self, session, vm, stop):
        self.app.status("connecting", "正在连接 Muse")
        run = asyncio.create_task(session.run(stop))
        ready = asyncio.create_task(session.registered.wait())
        outcome = Outcome.CLOSED
        error_type = http_status = None
        try:
            done, _ = await asyncio.wait((run, ready), return_when=asyncio.FIRST_COMPLETED)
            if ready in done and session.registered.is_set() and not run.done():
                self.app.session, self.app.vm = session, vm
                self.app.connection_state("connected")
            outcome = await run
            return outcome
        except Exception as exc:
            error_type = type(exc).__name__
            http_status = getattr(getattr(exc, "response", None), "status_code", None)
            raise
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
            await self.app.stop_call()
            await self.app.cancel_chat()
            self.app.session = self.app.vm = None
            if not self.app.stop.is_set():
                if outcome is Outcome.UNPAIRED:
                    self.app.paired = False
                    self.app.emit(dict(type="pairing.state", paired=False))
                    self.app.status("unpaired", "尚未配对 · S 打开设置")
                else:
                    self.app.connection_state("disconnected", http_status, outcome=outcome.value,
                                              error_type=error_type)


class App:
    def __init__(self, emit, demo=False):
        self.emit = emit
        self.demo = demo
        self.stop = asyncio.Event()
        self.session = self.vm = self.service = self.call = self.audio = None
        self.call_task = self.chat_task = self.service_task = self.pair_task = None
        self.call_lock = asyncio.Lock()
        self.voice_state = "idle"
        self.call_seq = 0
        self.call_started = 0.0
        self.setup_task = self.portal = None
        self.muted = False
        self.paired = demo
        self.view_mode = "idle"
        self.prefs = DEFAULTS.copy()
        self.trace = None
        if not demo:
            initialize_state()
            self.prefs.update({k: v for k, v in (config.load_json("preferences.json") or {}).items()
                               if k in DEFAULTS})
            self.prefs = validate_prefs(self.prefs)
            with contextlib.suppress(OSError):
                self.trace = Diagnostics(config.state_dir())
            with contextlib.suppress(OSError):
                config.save_json("voice-status.json",dict(stage="idle",reason="app_start",updated_at=int(time.time())))
        self.log("app.start",version=__version__,language=self.prefs["language"],state=self.voice_state)

    def log(self,event,**fields):
        if self.trace is not None:
            self.trace.record(event,**fields)

    def set_voice_state(self,state):
        if state=="live" and self.voice_state=="stopping":return
        self.voice_state=state
        self.emit(dict(type="call",active=state!="idle",phase=state))
        self.log("voice.state",state=state,call_seq=self.call_seq,muted=self.muted)

    def voice_metrics(self):
        a,c=self.audio,self.call
        result={"call_seq":self.call_seq,"muted":self.muted,"elapsed_seconds":round(max(0,time.monotonic()-self.call_started),2) if self.call_started else 0}
        for name in ("input_blocks","input_drops","output_blocks","callback_warnings","mic_level","mic_peak","speaker_level","speaker_peak"):
            result[name]=getattr(a,name,0) if a else 0
        result.update(input_rate=getattr(a,"input_rate",0),output_rate=getattr(a,"output_rate",0),
                      input_channels=getattr(a,"input_channels",0))
        result.update(input_default=self.prefs["input_device"] is None,output_default=self.prefs["output_device"] is None)
        result["capture_queue"]=a.capture.qsize() if a and hasattr(a,"capture") else 0
        result["playback_ms"]=round(getattr(a,"playback_bytes",0)*1000/(getattr(a,"output_rate",24000)*2),1) if a else 0
        for name in ("input_sent","input_voiced","input_gated_drops","audio_received_samples","audio_played_samples","audio_dropped_samples"):
            result[name]=getattr(c,name,0) if c else 0
        result.update(ready=bool(c and getattr(c,"ready",None) and c.ready.is_set()),server_hold=bool(getattr(c,"held",False)),mic_gated=bool(c and getattr(c,"_gated",lambda:False)()),sdk_playback_ms=round(getattr(c,"_playback_bytes",0)/48,1),active_turns=len(getattr(c,"_active_turns",{})))
        result["advisory_errors"]=getattr(c,"advisory_error_count",0)
        g=getattr(c,"_reply_gate",None)
        for key,attr in [("gate_waiting","waiting"),("gate_blocked","blocked"),("gate_confirmed","confirmed"),("gate_fenced","fenced")]:result[key]=bool(getattr(g,attr,False))
        for key,stream in [("input_active",getattr(a,"input",None)),("output_active",getattr(a,"output",None))]:
            result[key]=False
            if stream is not None:
                with contextlib.suppress(Exception):result[key]=bool(stream.active)
                with contextlib.suppress(Exception):result[key.replace("_active","_device")]=stream.device
        return result

    def status(self, mode, text):
        self.view_mode = mode
        self.emit(dict(type="status", mode=mode, text=translate(text, self.prefs["language"])))

    def error(self, code, message):
        self.emit(dict(type="error", code=code, message=translate(message, self.prefs["language"])))

    def operation_state(self, operation, stage, exc=None, reason=None):
        stages = {
            "chat": {"waiting_reply", "complete", "reply_missing", "failed"},
            "voice": {"vm_lookup", "transport", "audio_open", "admission", "live", "ended", "failed"},
        }
        if stage not in stages.get(operation, set()):
            return
        record = dict(stage=stage, updated_at=int(time.time()))
        if reason in ("cancelled","idle_timeout","overall_timeout","remote_end","remote_stop","failed","unknown"):record["reason"]=reason
        if exc is not None:
            record["error_type"] = type(exc).__name__
            status = getattr(exc,"status",None)
            if type(status) is int:
                record["http_status"] = status
        if not self.demo:
            with contextlib.suppress(OSError):
                config.save_json(operation+"-status.json",record)
        self.log("voice.error" if operation=="voice" and exc is not None else "voice.state" if operation=="voice" else "chat.state",**record,call_seq=self.call_seq)
        self.emit(dict(type=operation+".detail",**record))

    def publish_pairing_state(self):
        if not self.demo:
            record = config.load_json(config.PAIRING_FILE) or {}
            self.paired = bool(record.get("access_token") and record.get("refresh_token"))
        self.emit(dict(type="pairing.state", paired=self.paired))
        return self.paired

    def connection_state(self, stage, http_status=None, *, outcome=None, error_type=None):
        allowed = {"unpaired", "refresh_wait", "refreshing", "network_error", "no_muse", "http_error", "connecting", "connected", "disconnected"}
        if stage not in allowed:
            return
        self.publish_pairing_state()
        public_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        record = dict(stage=stage, http_status=public_status, paired=self.paired, updated_at=int(time.time()))
        if outcome is not None:
            record["outcome"] = outcome
        if error_type is not None:
            record["error_type"] = error_type
        if not self.demo:
            with contextlib.suppress(OSError):
                config.save_json("connection-status.json", record)
        self.emit(dict(type="connection.detail", **record))
        self.log("connection.state",**record)
        if not self.paired:
            self.status("unpaired", "尚未配对 · S 打开设置")
        elif stage == "connected":
            self.status("idle", "已连接 · 按空格开始")
        elif stage == "disconnected":
            self.status("offline", "已配对，连接断开，正在自动重连")
        elif stage == "network_error":
            self.status("offline", "已配对，暂时无法连接 Muse 服务，正在重试")
        elif stage in ("no_muse", "http_error", "refresh_wait"):
            self.status("offline", "已配对，正在等待 Muse 服务恢复")
        elif stage == "refreshing":
            self.status("connecting", "已配对，正在刷新设备授权")
        else:
            self.status("connecting", "已配对，正在连接 Muse")

    async def connect(self, retry=False):
        if self.demo:
            self.publish_pairing_state()
            self.status("idle", "演示模式 · 未连接服务")
            return
        if retry and self.service_task and not self.service_task.done():
            await self.stop_call()
            await self.cancel_chat()
            self.service.stop()
            await asyncio.gather(self.service_task, return_exceptions=True)
        self.publish_pairing_state()
        if self.service_task and not self.service_task.done():
            return
        if not self.paired:
            self.status("unpaired", "尚未配对 · S 打开设置")
            return
        self.status("connecting", "已配对，正在连接 Muse")
        self.service = AppService(identity.load_or_create(), None, sdk_token=config.sdk_token(), display_name="Muser")
        self.service.app = self
        self.service_task = asyncio.create_task(self.service.run())

    async def pair(self):
        if self.stop.is_set():
            return
        if self.demo:
            self.emit(dict(type="setup", url="http://192.168.1.42:8050/", expires=300))
            return
        if sys.platform != "linux":
            self.error("pairing_platform", "请在 Cardputer Zero 上使用手机 Muse App 配对")
            return
        if self.pair_task and not self.pair_task.done():
            return
        if self.setup_task and not self.setup_task.done():
            if self.portal:
                self.emit(dict(type="setup", url=self.portal.url, expires=max(0,int(self.portal.deadline-time.monotonic()))))
            return
        await self.stop_call()
        await self.cancel_chat()
        if self.service:
            self.service.stop()
        if self.service_task:
            await asyncio.gather(self.service_task, return_exceptions=True)
        if not config.sdk_token():
            try:
                self.portal = PhoneSetup(self.prefs["language"])
            except OSError:
                self.error("phone_network", "请先连接 Wi-Fi 或网线，并让手机加入同一局域网")
                return
            self.emit(dict(type="setup", url=self.portal.url, expires=300))
            self.setup_task = asyncio.create_task(self._phone_setup())
            return
        self.pair_task = asyncio.create_task(self._pair())

    async def _phone_setup(self):
        imported = False
        portal = self.portal
        try:
            imported = await portal.wait()
        finally:
            await asyncio.shield(portal.close())
            self.portal = self.setup_task = None
            self.emit(dict(type="setup.closed"))
        if imported:
            os.environ.pop(config.SDK_TOKEN_ENV, None)
            await self.pair()
        else:
            self.error("setup_expired", "手机设置已超时，请重新打开手机连接")

    async def cancel_pairing(self):
        for task in (self.setup_task, self.pair_task):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.portal:
            await self.portal.close()
            self.portal = None
        self.setup_task = self.pair_task = None
        self.emit(dict(type="setup.closed"))

    async def _pair(self):
        self.status("pairing", "正在准备蓝牙配对")
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve().parents[1] / "worker.py"),
            "--pair-only", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            async for line in proc.stdout:
                item = json.loads(line)
                self.emit(item)
                if item.get("type") == "pair.result":
                    self.publish_pairing_state()
                    if self.paired:
                        self.status("connecting", "配对已完成，正在连接 Muse")
            await proc.wait()
            if proc.returncode:
                self.error("pairing_failed", "配对未完成，请检查 token、蓝牙及手机开发者模式")
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        await self.connect()

    async def start_call(self):
        async with self.call_lock:
            await self._start_call()

    async def _start_call(self):
        self.log("voice.command",action="start",state=self.voice_state,call_seq=self.call_seq)
        if self.call_task and not self.call_task.done():
            self.log("voice.command",action="start_ignored",state=self.voice_state,call_seq=self.call_seq)
            return
        if self.chat_task and not self.chat_task.done():
            self.error("busy", "请等待文字回复完成")
            return
        if self.demo:
            self.call_seq+=1
            self.set_voice_state("live")
            self.status("listening", "演示 · 正在聆听")
            return
        if not self.session:
            self.error("not_connected", "已配对，正在连接 Muse，请稍候或重试连接" if self.publish_pairing_state() else "尚未连接，请先配对并等待连接")
            return
        self.call_seq+=1
        self.call_started=time.monotonic()
        self.set_voice_state("starting")
        self.call_task = asyncio.create_task(self._call(self.vm.copy()))

    async def _call(self, vm):
        voice_stop = asyncio.Event()
        connection = None
        levels = None
        self.status("connecting", "正在开启语音")
        stage = "vm_lookup"
        terminal_reason = "unknown"
        try:
            self.operation_state("voice",stage)
            ident = identity.load_or_create()
            paired = config.load_json(config.PAIRING_FILE)
            if not paired:
                raise ConnectionError("device is no longer paired")
            vms, status = await asyncio.to_thread(muse_api.fetch_vms_with_status,
                paired["access_token"], muse_api.api_root(paired.get("api_url_v2", "")))
            matches = [v for v in vms if v["vm_id"] == vm["vm_id"]]
            if status != 200 or len(matches) != 1:
                raise ConnectionError("exact Muse VM is unavailable")
            fresh = matches[0]
            stage = "transport"
            self.operation_state("voice",stage)
            voice = LinkSession(noise_host=paired.get("noise_host") or "hatch.metaaivm.com",
                vm_id=fresh["vm_id"], vm_auth_token=fresh["vm_auth_token"], register_device=False,
                app_id="hatch-web",
                device=DeviceDescription(ident.node_id, "Muser", __version__, {}),
                run_command=lambda *args: {"ok": False, "error": "unsupported command"})
            connection = asyncio.create_task(voice.run(voice_stop))
            await voice.wait_transport_ready(35)
            stage = "audio_open"
            self.operation_state("voice",stage)
            self.audio = Audio(self.prefs)
            self.audio.set_muted(self.muted)
            self.transcripts = Transcripts(self.emit)
            call_id = uuid.uuid4().hex
            original_emit = self.transcripts.emit
            self.transcripts.emit = lambda e: original_emit({**e, "id": call_id + ":" + e["id"]})
            def event(item):
                nonlocal stage
                self.transcripts.feed(item)
                kind = item.get("type")
                if kind == "hatch.voice.session":
                    stage = "live"
                    self.set_voice_state("live")
                    self.operation_state("voice",stage)
                if kind in Diagnostics.ENUM["protocol"]:
                    self.log("voice.protocol",protocol=kind,**self.voice_metrics())
                if self.voice_state=="stopping":return
                if kind in ("hatch.voice.session", "input_audio_buffer.speech_started"):
                    self.status("listening", "正在聆听")
                elif kind in ("input_audio_buffer.speech_stopped", "hatch.voice.turn_started"):
                    self.status("thinking", "Muse 正在思考")
            self.call = VoiceCall(voice, device_id=ident.node_id, half_duplex=True,
                idle_timeout=self.prefs["idle_timeout"], overall_timeout=self.prefs["overall_timeout"], on_event=event)
            levels = asyncio.create_task(self._levels())
            stage = "admission"
            self.operation_state("voice",stage)
            terminal_reason=await self.call.run(self.audio.frames(), self.audio, realtime_input=True)
            self.operation_state("voice","ended",reason=terminal_reason)
        except asyncio.CancelledError:
            terminal_reason="cancelled"
            self.operation_state("voice","ended",reason=terminal_reason)
            raise
        except Exception as exc:
            terminal_reason="failed"
            self.operation_state("voice",stage,exc,reason=terminal_reason)
            if stage == "audio_open":
                self.error(type(exc).__name__, "无法打开麦克风或扬声器，请检查设置中的音频设备")
            elif stage in ("vm_lookup","transport"):
                self.error(type(exc).__name__, "无法连接 Muse 语音服务，请稍后重试连接")
            elif stage == "admission":
                self.error(type(exc).__name__, "Muse 语音连接未就绪，请稍后重试")
            else:
                self.error(type(exc).__name__, "语音已中断，请检查音频设备和连接后重试")
        finally:
            if levels:
                levels.cancel()
                await asyncio.gather(levels, return_exceptions=True)
            self.log("voice.end",reason=terminal_reason,end_call_acknowledged=bool(getattr(self.call,"end_call_acknowledged",False)),**self.voice_metrics())
            if self.audio:
                self.log("voice.cleanup",stage="audio_close",call_seq=self.call_seq)
                try:self.audio.close()
                except Exception as exc:self.log("voice.error",stage="audio_close",error_type=type(exc).__name__,call_seq=self.call_seq)
            self.audio = self.call = None
            voice_stop.set()
            self.log("voice.cleanup",stage="transport_close",call_seq=self.call_seq)
            if connection:
                try:
                    await asyncio.wait_for(connection, 3)
                except (Exception, asyncio.CancelledError):
                    connection.cancel()
                    await asyncio.gather(connection, return_exceptions=True)
            self.log("voice.cleanup",stage="cleanup_complete",call_seq=self.call_seq)
            self.set_voice_state("idle")
            self.status("idle" if self.session else "connecting", "通话已结束 · 空格重新开始")

    async def _levels(self):
        quiet_since = time.monotonic()
        last_diagnostic=0.0
        while True:
            if self.audio:
                level = self.audio.speaker_level
                if level > .025 and self.voice_state!="stopping":
                    quiet_since = time.monotonic()
                    if self.view_mode != "speaking":
                        self.status("speaking", "Muse 正在说话")
                elif self.voice_state!="stopping" and self.view_mode == "speaking" and time.monotonic() - quiet_since > .65:
                    self.status("listening", "正在聆听")
                self.emit(dict(type="level", playback=level, mic=self.audio.mic_level))
                if time.monotonic()-last_diagnostic>=5:
                    self.log("voice.stats",state=self.voice_state,**self.voice_metrics())
                    last_diagnostic=time.monotonic()
            await asyncio.sleep(.08)

    async def stop_call(self):
        async with self.call_lock:
            task=self.call_task
            self.log("voice.command",action="stop",state=self.voice_state,call_seq=self.call_seq)
            if task and not task.done():
                self.set_voice_state("stopping")
                self.status("ending","正在结束语音，请稍候")
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            if self.call_task is task:self.call_task=None
            self.set_voice_state("idle")
            if self.demo:self.status("idle","演示模式 · 未连接服务")

    async def cancel_chat(self):
        if self.chat_task and not self.chat_task.done():
            self.chat_task.cancel()
            await asyncio.gather(self.chat_task, return_exceptions=True)
        self.chat_task = None

    async def chat(self, text):
        await self.stop_call()
        if self.chat_task and not self.chat_task.done():
            self.error("busy", "请等待上一条文字回复")
            return
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 4096:
            self.error("invalid_text", "请输入 1–4096 字节的文字")
            return
        if not self.demo and not self.session:
            self.error("not_connected", "已配对，正在连接 Muse，请稍候或重试连接" if self.publish_pairing_state() else "尚未连接，请先配对")
            return
        mid = uuid.uuid4().hex
        self.emit(dict(type="transcript", id=mid, role="user", text=text.strip(), final=True))
        self.chat_task = asyncio.create_task(self._chat(text.strip(), mid))

    async def _chat(self, text, mid):
        self.status("thinking", "Muse 正在思考")
        if self.demo:
            await asyncio.sleep(.25)
            self.emit(dict(type="transcript", id=mid + "-reply", role="assistant",
                text=translate("这是演示回复。真机配对后，可通过语音或键盘与 Muse 交流。", self.prefs["language"]), final=True))
            self.status("idle", "演示模式 · 未连接服务")
            return
        def publish(update):
            if isinstance(update, Reply) and update.text:
                self.emit(dict(type="transcript", id=update.message_id, role="assistant", text=update.text, final=update.done))
            elif isinstance(update, AgentStatus):
                self.status("thinking", "Muse 正在处理")
        try:
            self.operation_state("chat","waiting_reply")
            session = self.session
            result = await send_and_wait(session,
                lambda: session.send_chat(text, output_modality="text"),None,120,on_update=publish)
            if not result.get("ok") or result.get("reply") is None:
                self.operation_state("chat","reply_missing")
                self.error("chat_reply_missing", "文字回复暂未收到，消息不会自动重复发送")
            else:
                self.operation_state("chat","complete")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.operation_state("chat","failed",exc)
            self.error(type(exc).__name__, "文字回复未完成，请检查连接后重试")
        finally:
            self.status("idle", "已连接 · 按空格开始")

    async def settings(self, update=None):
        if update:
            self.prefs.update(validate_prefs({**self.prefs, **update}))
            if not self.demo:
                config.save_json("preferences.json", self.prefs)
        devices = []
        if not self.demo:
            try:
                import sounddevice
                devices = [dict(id=i, name=d["name"], input=d["max_input_channels"] > 0,
                                output=d["max_output_channels"] > 0)
                           for i, d in enumerate(sounddevice.query_devices())]
            except Exception:
                self.error("audio_devices", "无法读取音频设备，请检查 PortAudio")
        self.emit(dict(type="settings", **self.prefs, muted=self.muted, devices=devices))

    async def command(self, item):
        op = item.get("command")
        if op == "connect":
            await self.connect(retry=True)
        elif op == "pair":
            await self.pair()
        elif op == "pair.cancel":
            await self.cancel_pairing()
        elif op == "call.start":
            await self.start_call()
        elif op == "call.stop" or op == "compose":
            await self.stop_call()
            if op == "compose":
                self.emit(dict(type="compose.ready"))
        elif op == "call.interrupt":
            self.log("voice.command",action="interrupt",state=self.voice_state,call_seq=self.call_seq)
            if self.call:
                self.transcripts.interrupt()
                await self.call.barge_in()
                self.status("listening", "已打断 · 请继续说话")
        elif op == "mic.mute":
            self.muted = bool(item.get("muted"))
            self.log("voice.command",action="mute",state=self.voice_state,call_seq=self.call_seq,muted=self.muted)
            if self.audio:
                self.audio.set_muted(self.muted)
            self.emit(dict(type="muted", value=self.muted))
        elif op == "chat.send":
            await self.chat(item.get("text"))
        elif op == "clear":
            await self.stop_call()
            await self.cancel_chat()
            self.emit(dict(type="cleared"))
        elif op == "settings":
            await self.settings(item.get("values"))
        elif op == "shutdown":
            self.stop.set()
        else:
            self.error("unknown_command", "未知操作")

    async def shutdown(self):
        await self.cancel_pairing()
        await self.stop_call()
        await self.cancel_chat()
        if self.pair_task:
            self.pair_task.cancel()
            await asyncio.gather(self.pair_task, return_exceptions=True)
        if self.service:
            self.service.stop()
        if self.service_task:
            self.service_task.cancel()
            await asyncio.gather(self.service_task, return_exceptions=True)
        self.log("app.stop")
        if self.trace is not None:self.trace.close()


def validate_prefs(values):
    prefs = {k: values.get(k, v) for k, v in DEFAULTS.items()}
    if prefs["language"] not in ("en", "zh-CN"):
        raise ValueError("unsupported language")
    for key, low, high in (("volume", 0, 100), ("idle_timeout", 10, 3600), ("overall_timeout", 60, 7200)):
        value = prefs[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError("invalid preference")
        prefs[key] = int(value)
    for key in ("input_device", "output_device"):
        value = prefs[key]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("invalid device")
    return prefs


def emit(item):
    print(json.dumps(item, ensure_ascii=False, separators=(",", ":")), flush=True)


async def run(demo=False):
    app = App(emit, demo)
    reader = asyncio.StreamReader(limit=65536)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, app.stop.set)
    async def commands():
        while not app.stop.is_set():
            try:
                line = await reader.readline()
                if not line:
                    app.stop.set()
                    return
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError()
                await app.command(item)
            except (ValueError, TypeError, KeyError):
                app.error("invalid_request", "操作参数无效")
            except Exception as exc:
                app.error(type(exc).__name__, "操作失败，请检查设置与设备连接")
    listener = asyncio.create_task(commands())
    try:
        emit(dict(type="hello", app="Muser", protocol=1, demo=demo))
        emit(dict(type="settings", **app.prefs, muted=app.muted, devices=[]))
        await app.connect()
        await app.stop.wait()
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await app.shutdown()
        transport.close()
        emit(dict(type="stopped"))


def main():
    logging.disable(logging.CRITICAL)  # SDK/network exception bodies may contain private data.
    parser = argparse.ArgumentParser(description="Muser device backend")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--pair-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.pair_only:
            initialize_state()
            from musegadget.pair import pair, PairOutcome
            result = pair(force=True, on_open=lambda w: emit(dict(type="pairing", name=w.ble_name, timeout=w.timeout_s)))
            emit(dict(type="pair.result", outcome=result.outcome.value))
            if result.outcome not in (PairOutcome.PAIRED, PairOutcome.ALREADY_PAIRED):
                sys.exit(1)
        else:
            asyncio.run(run(args.demo))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        emit(dict(type="error", code=type(exc).__name__, message="Muser could not start. Check configuration and system dependencies."))
        sys.exit(1)
