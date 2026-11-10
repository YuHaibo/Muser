#!/usr/bin/env python3
"""Check local release metadata/assets. Artifact checks run separately with dpkg/readelf."""
import json
from pathlib import Path
import re
from PIL import Image

root=Path(__file__).resolve().parents[1]
meta=json.loads((root/"app-builder.json").read_text())
assert meta["app_name"]=="Muser" and meta["package_name"]=="muser"
assert meta["store"]["share_code"]=="MUSE"
assert meta["version"]=="1.0.0"
assert meta["store"]["license"]=="Apache-2.0"
assert len(meta["store"]["screenshots"])==4
for path in meta["store"]["screenshots"]:
    im=Image.open(root/path)
    assert im.size==(320,170),path
assert Image.open(root/meta["store"]["icon"]).size==(256,256)
desktop=(root/"packaging/muser.desktop").read_text()
assert "Name=Muser\n" in desktop and "Terminal=false\n" in desktop
assert "share/images/muser.png" in desktop
assert "not affiliated with Meta" in (root/"README.md").read_text()
assert "Apache License" in (root/"LICENSE").read_text()
assert (root/"NOTICE").is_file() and (root/"THIRD_PARTY_LICENSES.md").is_file()
for removed in ("HANDOFF.md","VALIDATION.md","PUBLISH.md"):
    assert not (root/removed).exists(),removed
for base in ("src","backend","packaging","assets"):
    for p in (root/base).rglob("*"):
        if p.is_file():
            assert not re.search(rb"mgst_[A-Za-z0-9_-]{43}",p.read_bytes()),str(p)
assert not list((root/"backend").rglob("pairing.json"))
assert not (root/"packaging/muser-setup").exists()
assert (root/"assets/branding/muser-logo.png").is_file()
print("Muser release metadata, four screenshots, icon, attribution and credential exclusion verified")
