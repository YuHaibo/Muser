# Third-party components and licenses

Muser is licensed under the Apache License, Version 2.0 (see [LICENSE](LICENSE)).
That license applies only to original Muser code. It does not grant rights to
third-party trademarks, characters or artwork.

| Component | Source / pin | License / notice |
| --- | --- | --- |
| Cardputer Zero Template structure and cross toolchain | `CardputerZero/Template@477fa6d217b683e3084c361a757f03dc8ca9f753` | MIT; Copyright (c) 2026 M5Stack CardputerZero Community, retained in `vendor/TEMPLATE-LICENSE` |
| LVGL | `lvgl/lvgl@v9.5.0` | MIT, fetched at build time; `vendor/LVGL-LICENSE` |
| Muse Linux SDK, voice helpers and headers | `facebookincubator/muse-gadget-sdk@463bd2a05380fea4211b0099ad0f70933c108b20` | Apache-2.0; `vendor/MUSE-SDK-LICENSE` |
| Default Muse / Jollybot pixel avatar | Same SDK, `esp32/avatar/muse_pixel.c` and headers | **Excluded from the upstream Apache License.** Original Meta copyright retained. Muser does not relicense this artwork; see the avatar note below. |
| nlohmann/json | v3.11.3 single header | MIT; `vendor/JSON-LICENSE` |
| websockets | 15.0.1, universal wheel | BSD-3-Clause, bundled dist-info license |
| sounddevice | 0.5.3, universal wheel | MIT, bundled dist-info license |
| Noto Sans CJK SC | `notofonts/noto-cjk`, `Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf` | SIL Open Font License 1.1; `assets/fonts/OFL.txt` |

## Muse SDK modifications

The following local changes are carried on top of the pinned SDK:

- `VoiceCall` accepts an optional synchronous `on_event` observer after the
  reply gate handles each validated event.
- `Service.on_connection_state(stage, http_status)` exposes connection phases
  to the UI, and `send_and_wait(..., on_update=...)` exposes typed updates.
- The reply follower tracks persisted user-message sequences separately from
  live status and text deltas while retaining the acknowledged-message fence.
- The voice transport exposes numerical frame counters for metadata
  diagnostics and schedules live uploads without accumulating send time in
  each frame interval.

The original SDK tests are retained under `tests/upstream`, with the example
fixtures they need under `tests/examples`.

## Avatar notice

The upstream Muse Gadget SDK README states: "The Apache License does not cover
the Jollybot avatar." The avatar files vendored here (`vendor/muse_pixel.c`,
`vendor/muse_pixel.h`, `vendor/muse_speaking.h`, `vendor/muse_state.h`) are
included only as a pinned SDK dependency. They are not relicensed by Muser,
and this project claims no rights in the character design or artwork. Review
the upstream terms before redistributing the avatar or using it commercially.

## Python wheels

`dependencies.lock.json` records the pinned Python wheel hashes. The runtime
uses the distribution packages for cryptography, CFFI, D-Bus and GI bindings
on Linux. No proprietary service credentials are part of this repository.

## Trademarks

Muse and related marks belong to Meta and its affiliates. Muser is an
independent, unofficial project and is not affiliated with, endorsed by or
sponsored by Meta. These names are used only to describe compatibility with
the Muse service.
