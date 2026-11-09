"""Bounded voice transcript assembly, final correction and interruption fencing."""
import re


class Transcripts:
    def __init__(self, emit):
        self.emit = emit
        self.messages = {}
        self.seen = set()
        self.stale = set()
        self.response = None
        self.waiting = False
        self.input_item = None

    def interrupt(self):
        self.stale.update(k.split(":", 1)[1] for k, v in self.messages.items() if v["role"] == "assistant")
        if self.response:
            self.stale.add(self.response)
        self.waiting = True
        self.input_item = None

    def feed(self, event):
        kind = event.get("type", "")
        if kind not in {"input_audio_buffer.speech_started", "hatch.voice.turn_started", "response.created",
                        "conversation.item.input_audio_transcription.completed",
                        "response.output_audio_transcript.delta", "response.output_audio_transcript.done"}:
            return
        eid = event.get("event_id")
        if eid:
            if eid in self.seen:
                return
            if len(self.seen) >= 4096:
                raise ValueError("transcript event limit")
            self.seen.add(eid)
        if kind == "input_audio_buffer.speech_started":
            self.interrupt()
            self.input_item = event.get("item_id")
        elif kind == "hatch.voice.turn_started":
            if self.input_item and event.get("item_id") == self.input_item:
                self.waiting = False
        elif kind == "response.created":
            self.response = event.get("response", {}).get("id")
        elif kind == "conversation.item.input_audio_transcription.completed":
            self._update("user", event.get("item_id"), event.get("transcript", ""), True)
        elif kind in ("response.output_audio_transcript.delta", "response.output_audio_transcript.done"):
            mid = event.get("response_id") or self.response
            if self.waiting or mid in self.stale:
                return
            done = kind.endswith(".done")
            self._update("assistant", mid, event.get("transcript" if done else "delta", ""), done)

    def _update(self, role, mid, text, done):
        if not isinstance(mid, str) or not isinstance(text, str):
            return
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
        key = role + ":" + mid
        old = self.messages.get(key, {"role": role, "text": "", "final": False})
        if old["final"]:
            return
        if len(self.messages) >= 256 and key not in self.messages:
            raise ValueError("transcript message limit")
        value = text if done or role == "user" else old["text"] + text
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("transcript text limit")
        self.messages[key] = {"role": role, "text": value, "final": done}
        self.emit({"type": "transcript", "id": key, **self.messages[key]})
