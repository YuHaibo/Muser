"""Short-lived, one-use LAN page for pasting an SDK token from a phone."""
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import parse_qs

from musegadget import config


def local_address():
    override = os.environ.get("MUSER_SETUP_HOST")
    if override:
        return str(ipaddress.IPv4Address(override))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # Route lookup only; no packet is sent.
        address = probe.getsockname()[0]
    if ipaddress.ip_address(address).is_loopback:
        raise OSError("Wi-Fi or Ethernet is required for phone setup")
    return address


def save_sdk_token(token):
    if not isinstance(token, str) or not config._SDK_TOKEN.fullmatch(token.strip()):
        raise ValueError("invalid SDK token")
    directory = config.state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sdk-token-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(token.strip() + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / config.SDK_TOKEN_FILE)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


class PhoneSetup:
    def __init__(self, language="en", host=None, lifetime=300):
        self.language = language
        self.host = host or local_address()
        self.deadline = time.monotonic() + lifetime
        self.path = "/"
        self.imported = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # URLs and request bodies never go to logs.

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def reply(self, status, content):
                body = content.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "same-origin")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)

            def allowed(self):
                return time.monotonic() < owner.deadline and not owner.imported.is_set()

            def do_GET(self):
                if not self.allowed():
                    self.reply(404, "Setup link expired or unavailable.")
                    return
                self.reply(200, owner.page())

            def do_POST(self):
                if not self.allowed():
                    self.reply(404, "Setup link expired or unavailable.")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 256 or not self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                        raise ValueError()
                    form = parse_qs(self.rfile.read(size).decode("ascii"), max_num_fields=2)
                    tokens = form.get("token", [])
                    if len(tokens) != 1:
                        raise ValueError()
                    save_sdk_token(tokens[0])
                except (ValueError, UnicodeError, OSError):
                    self.reply(400, "Invalid SDK token. Return and paste the complete token from Muse Gadgets.")
                    return
                owner.imported.set()
                self.reply(200, owner.page(done=True))

        self.server = HTTPServer((self.host, 0), Handler)
        self.authority = self.host + ":" + str(self.server.server_port)
        self.url = "http://" + self.authority + self.path
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.thread.start()
        self.closed = False
        self.close_lock = asyncio.Lock()

    def page(self, done=False):
        zh = self.language == "zh-CN"
        title = "连接 Muser" if zh else "Connect Muser"
        if done:
            content = ("<h1>已导入</h1><p>请返回 Cardputer Zero，按屏幕提示打开 Muse App，通过蓝牙添加设备。现在可以关闭本页。</p>" if zh else
                       "<h1>Ready to pair</h1><p>Return to Cardputer Zero. Follow its instructions to add the device over Bluetooth in the Muse app. You can close this page.</p>")
        else:
            content = ("<h1>连接设置</h1><p>手机或电脑均可打开本页，设备端无需输入长串字符。</p><ol><li>打开官方页面，创建或复制你的 SDK token。</li><li>返回本页粘贴，然后点击“导入并配对”。</li><li>接着使用 Muse App 蓝牙添加设备；如出现 Wi-Fi 选择，选择设备当前的网络。</li></ol>" if zh else
                       "<h1>Connection setup</h1><p>Use any phone or computer. No typing on your device.</p><ol><li>Open Muse Gadgets and create or copy your SDK token.</li><li>Return here, paste it, then choose Import &amp; pair.</li><li>Add your device over Bluetooth in the Muse app. If asked for Wi-Fi, select the device's current network.</li></ol>")
            content += '<a class="link" href="https://gadgets.muse.ai/settings/sdk-tokens" target="_blank" rel="noopener noreferrer">' + ("打开 Muse Gadgets" if zh else "Open Muse Gadgets") + '</a>'
            content += '<form method="post" action="/"><label for="token">SDK token</label><input id="token" name="token" type="password" maxlength="64" placeholder="Paste SDK token" autocomplete="off" autocapitalize="none" spellcheck="false" required><button type="submit">' + ("导入并配对" if zh else "Import &amp; pair") + '</button></form>'
            content += '<p class="note">' + ("仅保存在你的设备。提交成功后服务关闭，5 分钟未完成或退出设置也会关闭。手机或电脑与设备应连接同一局域网。" if zh else "Saved only on your device. The server closes after a successful import, after 5 minutes, or when setup is closed. Use the same local network as your device.") + '</p>'
        return '<!doctype html><html lang="' + ("zh-CN" if zh else "en") + '"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Muser</title><style>body{margin:0;background:#0c1015;color:#e8eaf0;font:16px/1.6 system-ui,sans-serif}main{max-width:440px;margin:auto;padding:32px 22px}header{color:#4cabff;letter-spacing:.12em;font-size:14px}h1{font-size:28px;margin:24px 0 10px}li{margin:10px 0}a{color:#69bbff}input,button{box-sizing:border-box;width:100%;padding:14px;font:inherit;border-radius:6px;margin:8px 0 12px}input{background:#141d29;color:#fff;border:1px solid #445267}button{background:#4cabff;border:0;color:#07111d;font-weight:600}form{margin-top:24px}.note{color:#9aa6b4;font-size:13px}</style><main><header>MUSER / ' + title + '</header>' + content + '</main></html>'

    async def wait(self):
        try:
            while time.monotonic() < self.deadline:
                if self.imported.is_set():
                    return True
                await asyncio.sleep(.1)
            return False
        finally:
            # Always close the listening socket before returning to BLE pairing.
            await asyncio.shield(self.close())

    async def close(self):
        async with self.close_lock:
            if not self.closed:
                try:
                    await asyncio.to_thread(self.server.shutdown)
                finally:
                    self.server.server_close()
                    await asyncio.to_thread(self.thread.join, 4)
                    self.closed = True
