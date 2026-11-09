import asyncio

from muser.worker import App
from musegadget import muse_turn


def event(name,mid=None,**payload):
    if mid is not None:payload["message_id"]=mid
    return {"type":"event","event":name,"payload":{"session_id":"main",**payload}}


class Session:
    def __init__(self,ack_delay=0):
        self.queue=asyncio.Queue()
        self.open=0
        self.sent=0
        self.producer=None
        self.ack_delay=ack_delay
    async def subscribe_chat(self,session_id=None,*,opened=None):
        self.open+=1
        opened.set()
        try:
            # Subscription snapshots refer to an older finished task.
            yield dict(event("agent.status",activity_code="idle"), seq=100)
            yield dict(event("task.status",status="completed",task_id="old-task"), seq=101)
            while True:yield await self.queue.get()
        finally:self.open-=1
    async def send_chat(self,text,*,output_modality):
        assert self.open==1 and output_modality=="text"
        self.sent+=1
        async def reply():
            await asyncio.sleep(.06)
            messages=[event("message.user","own-input",role="user"),
                event("delta.message_start","own-reply"),
                event("delta.text_append","own-reply",parent_message_id="own-reply",text="Fresh "),
                event("delta.text_append","own-reply",parent_message_id="own-reply",text="reply"),
                event("delta.message_done","own-reply",display_text="Fresh reply"),
                event("task.status",status="completed",task_id="own-task")]
            # The persisted user-message sequence trails the live status stream.
            for seq, message in zip((70, 102, 103, 104, 105, 106), messages):
                self.queue.put_nowait(dict(message, seq=seq))
        self.producer=asyncio.create_task(reply())
        await asyncio.sleep(self.ack_delay)
        return {"ok":True,"status":200,"response":{"message_id":"own-input","session_id":"main"}}


def run_reply(tmp_path,monkeypatch,ack_delay):
    monkeypatch.setenv("MUSER_STATE_DIR",str(tmp_path/"state"))
    monkeypatch.setattr(muse_turn,"REPLY_QUIET_S",.02)
    monkeypatch.setattr(muse_turn,"EMPTY_REPLY_GRACE_S",.02)
    output=[]
    async def scenario():
        app=App(output.append)
        session=Session(ack_delay)
        app.session=session
        await asyncio.wait_for(app._chat("synthetic input","ui-local-id"),2)
        await session.producer
        assert session.sent==1 and session.open==0
        replies=[e for e in output if e["type"]=="transcript" and e["role"]=="assistant"]
        assert [e["text"] for e in replies]==["Fresh ","Fresh reply","Fresh reply"]
        assert replies[-1]["final"]
        assert not any(e["type"]=="error" for e in output)
        await app.shutdown()
    asyncio.run(scenario())


def test_subscription_completed_snapshot_does_not_end_new_reply(tmp_path,monkeypatch):
    run_reply(tmp_path,monkeypatch,0)


def test_reply_arriving_before_ack_is_released_to_the_ui(tmp_path,monkeypatch):
    run_reply(tmp_path,monkeypatch,.1)
