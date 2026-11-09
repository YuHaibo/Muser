"""PortAudio adapter: bounded capture, native hardware rates, PCM24k protocol."""
import asyncio
import math
from pathlib import Path
import queue
import struct
import threading
from collections import deque
from musegadget.voice_audio import Pcm48To24


CARDPUTER_AUDIO = Path("/proc/asound/ES8389Audio/id")


class Resampler:
    """Streaming linear resampler; 48k capture uses the SDK's FIR instead."""
    def __init__(self, source, target):
        self.step = source / target
        self.samples = []
        self.position = 0.0

    def feed(self, pcm):
        if self.step == 1:
            return pcm
        self.samples.extend(struct.unpack("<" + "h" * (len(pcm) // 2), pcm))
        output = []
        while self.position + 1 < len(self.samples):
            i = int(self.position)
            f = self.position - i
            output.append(round(self.samples[i] * (1 - f) + self.samples[i + 1] * f))
            self.position += self.step
        consumed = min(int(self.position), max(0, len(self.samples) - 1))
        del self.samples[:consumed]
        self.position -= consumed
        return struct.pack("<" + "h" * len(output), *output)


MIC_GAIN = 4.0
MIC_HIGHPASS_HZ = 120.0
MIC_PEAK = 30000


def rms(pcm):
    return levels(pcm)[0]


def levels(pcm):
    if not pcm:
        return 0.0,0.0
    values = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    return (min(1.0,math.sqrt(sum(v*v for v in values)/len(values))/10000),max(abs(v) for v in values)/32768)


class MicShaper:
    """Remove rumble and lift the board microphone before the protocol.

    The ES8389 route delivers quiet, low-frequency-heavy speech: measured
    speech sat near RMS 150-300 while 135 Hz room rumble sat at a similar
    level, enough to trip the provider VAD without producing a usable turn.
    A one-pole high-pass drops the rumble and a bounded makeup gain brings
    normal speech to the level that direct synthetic input already proved
    usable. The per-block peak limit keeps loud input from clipping.
    """
    def __init__(self, highpass_hz=MIC_HIGHPASS_HZ, gain=MIC_GAIN):
        self.alpha = math.exp(-2 * math.pi * highpass_hz / 24000)
        self.gain = gain
        self.previous_in = self.previous_out = 0.0

    def feed(self, pcm):
        if not pcm:
            return pcm
        shaped = []
        previous_in, previous_out = self.previous_in, self.previous_out
        for value in struct.unpack("<" + "h" * (len(pcm) // 2), pcm):
            previous_out = value - previous_in + self.alpha * previous_out
            previous_in = value
            shaped.append(previous_out)
        self.previous_in, self.previous_out = previous_in, previous_out
        peak = max(abs(value) for value in shaped) or 1.0
        gain = min(self.gain, MIC_PEAK / peak)
        shaped = [max(-32768, min(32767, round(value * gain))) for value in shaped]
        return struct.pack("<" + "h" * len(shaped), *shaped)


class Audio:
    def __init__(self, prefs, sound=None):
        if sound is None:
            import sounddevice as sound
        self.prefs = prefs
        self.capture = queue.Queue(maxsize=8)
        self.playback = deque()
        self.playback_bytes = 0
        self.lock = threading.Lock()
        self.mic_level = self.speaker_level = 0.0
        self.failure = False
        self.callback_warnings = 0
        self.input_blocks = self.input_drops = self.output_blocks = 0
        self.mic_peak = self.speaker_peak = 0.0
        self.streams = []
        self.tail = bytearray()
        self.muted = False
        try:
            input_device = prefs["input_device"]
            # The board's default route exposes two channels, but its built-in
            # mic feeds one. PipeWire's mono mix attenuates that channel.
            channel_options = (2, 1) if input_device is None and CARDPUTER_AUDIO.is_file() else (1,)
            for channels in channel_options:
                try:
                    ir = self._rate(sound, "input", input_device, channels=channels)
                    self.input = sound.RawInputStream(device=input_device, samplerate=ir,
                        channels=channels, dtype="int16", blocksize=ir // 25, callback=self._capture)
                except (OSError, sound.PortAudioError):
                    if channels == 1:
                        raise
                else:
                    self.input_channels = channels
                    break
            self.streams.append(self.input)
            outr = self._rate(sound, "output", prefs["output_device"])
            self.input_rate,self.output_rate=ir,outr
            self.down = Pcm48To24() if ir == 48000 else Resampler(ir, 24000)
            self.up = Resampler(24000, outr)
            self.mic = MicShaper()
            self.max_playback = outr * 2  # one second; VoiceCall has its own bounded queue
            self.output = sound.RawOutputStream(device=prefs["output_device"], samplerate=outr,
                channels=1, dtype="int16", blocksize=outr // 25, callback=self._play)
            self.streams.append(self.output)
            for stream in self.streams:
                stream.start()
        except BaseException:
            try:self.close()
            except Exception:pass
            raise

    @staticmethod
    def _rate(sound, kind, device, channels=1):
        info = sound.query_devices(device, kind)
        check = getattr(sound, "check_" + kind + "_settings")
        # The default PipeWire route can resample to the protocol rate.
        # Prefer that over running the 48 kHz FIR in the Python callback.
        for rate in dict.fromkeys((24000, 48000, int(info["default_samplerate"]))):
            try:
                check(device=device, channels=channels, dtype="int16", samplerate=rate)
                return rate
            except Exception:
                pass
        raise OSError("unsupported audio device")

    def _capture(self, data, frames, timing, status):
        self.input_blocks=getattr(self,"input_blocks",0)+1
        if status:
            self.callback_warnings = getattr(self,"callback_warnings",0)+1
        pcm = bytes(data)
        if self.input_channels == 2:
            samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
            channel = max((samples[::2], samples[1::2]), key=lambda s: sum(x * x for x in s))
            pcm = struct.pack(f"<{len(channel)}h", *channel)
        pcm = self.down.feed(pcm)
        pcm = self.mic.feed(pcm)
        self.mic_level,self.mic_peak=levels(pcm) if not self.muted else (0.0,0.0)
        if self.muted:
            pcm = bytes(len(pcm))
        if self.capture.full():
            self.input_drops=getattr(self,"input_drops",0)+1
            try:
                self.capture.get_nowait()
            except queue.Empty:
                pass
        try:
            self.capture.put_nowait(pcm)
        except queue.Full:
            pass

    def _play(self, data, frames, timing, status):
        self.output_blocks=getattr(self,"output_blocks",0)+1
        if status:
            self.callback_warnings = getattr(self,"callback_warnings",0)+1
        result = bytearray(len(data))
        offset = 0
        with self.lock:
            while self.playback and offset < len(result):
                pcm = self.playback[0]
                count = min(len(pcm), len(result) - offset)
                result[offset:offset + count] = pcm[:count]
                self.playback_bytes -= count
                offset += count
                if count == len(pcm):
                    self.playback.popleft()
                else:
                    self.playback[0] = pcm[count:]
        gain = self.prefs["volume"] / 100
        values = struct.unpack("<" + "h" * (len(result) // 2), result)
        result = struct.pack("<" + "h" * len(values), *(round(v * gain) for v in values))
        self.speaker_level,self.speaker_peak = levels(result)
        data[:] = result

    async def frames(self):
        # VoiceCall starts consuming after admission. Discard the audio queued
        # while the connection was opening, before sending live microphone input.
        self._clear_capture()
        while True:
            if self.failure or any(not s.active for s in self.streams):
                raise OSError("audio device interrupted")
            try:
                self.tail.extend(self.capture.get_nowait())
            except queue.Empty:
                await asyncio.sleep(.01)
                continue
            while len(self.tail) >= 1920:
                frame = bytes(self.tail[:1920])
                del self.tail[:1920]
                yield bytes(1920) if self.muted else frame

    def write(self, pcm):
        pcm = self.up.feed(pcm)
        with self.lock:
            if self.playback_bytes + len(pcm) > self.max_playback:
                raise OSError("audio playback stalled")
            self.playback.append(pcm)
            self.playback_bytes += len(pcm)

    def set_muted(self, muted):
        self.muted = muted
        self._clear_capture()

    def _clear_capture(self):
        self.tail.clear()
        while True:
            try:
                self.capture.get_nowait()
            except queue.Empty:
                return

    def flush(self):
        with self.lock:
            self.playback.clear()
            self.playback_bytes = 0
        self.speaker_level = 0

    def close(self):
        # Always release every stream, even if one abort/close fails.
        streams,self.streams=self.streams,[]
        first_error=None
        self.set_muted(True)
        self.flush()
        for stream in reversed(streams):
            for action in (stream.abort,stream.close):
                try:action()
                except Exception as exc:
                    if first_error is None:first_error=exc
        self.mic_level=self.mic_peak=self.speaker_level=self.speaker_peak=0.0
        if first_error is not None:raise first_error
