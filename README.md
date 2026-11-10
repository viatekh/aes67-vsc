# aes67-vsc — AES67 + Dante virtual soundcard

A plug-and-play network sound card for Windows, macOS and Linux. It works with:
- **Dante** devices, natively (experimental) or via their AES67 mode
- **RAVENNA** gear (Merging, Lawo, DirectOut, Neumann…)
- **NMOS** broadcast controllers
- any other AES67 device

No Audinate license is needed: it uses the open AES67 standard, plus an optional unofficial native Dante mode.

## Install
Download the installer for your OS from the Releases page:
- **Windows:** `aes67-vsc-setup.exe`. It also adds firewall rules and an optional start-at-login.
- **macOS:** `aes67-vsc-macos.dmg`.
- **Linux:** `aes67-vsc-linux.tar.gz`, then run `./install-linux.sh`. That lets clock sync run without sudo.

Or run it from source:
```
pip install -e .
aes67-vsc            # opens the tray icon + control panel at http://127.0.0.1:8067
```

## What happens automatically
| | |
|---|---|
| **Network** | Picks the interface where PTP/AES67 traffic is seen. Prefers wired over Wi-Fi, and real network cards over virtual ones. |
| **Clock** | Follows the best PTP clock on the network, e.g. a Dante leader. If there is none, this computer becomes the leader, so it also works PC-to-PC. |
| **Discovery** | Finds streams via SAP (Dante, most AES67) and mDNS/RTSP (RAVENNA), and announces its own streams both ways. |
| **NMOS** | Runs an IS-04 node and registers with any NMOS registry it finds. IS-05 lets controllers patch its receivers. |
| **Reconnect** | Receivers connect when their stream appears, and reconnect if it moves or restarts. |
| **Clock drift** | Corrects drift with smooth resampling (±5000 ppm range), so there are no clicks or dropouts. |
| **Sound card** | Device "Virtual sound card" shows up in your other apps, see below. |

## Native Dante mode (experimental)
Turn on **Dante → "Appear in Dante Controller"** in the control panel. This computer then shows up in
Dante Controller as a regular Dante device, with TX and RX channels you route in the normal grid.
- No AES67 mode is needed on your Dante gear.
- Other apps reach those channels through the virtual sound card.

What works:
- Routing in both directions, unicast and multicast flows, 16/24/32-bit, 44.1–192 kHz.
- Renaming the device and its channels, changing latency and sample rate, level meters, all from Dante Controller.
- Names and routing survive a restart, like on real devices.
- Clock: follows the Dante network clock (PTPv1). If there is no Dante device, the app provides the clock itself,
  so two computers running this app can also work alone.

How it works:
- The app speaks Dante's own discovery, control and audio protocols.
- The code is a Python port of [Inferno](https://github.com/teodly/inferno), an open-source,
  reverse-engineered Dante implementation. That's why this project is GPL.

**Important:**
- This is **unofficial**. Dante is Audinate's trademark and undocumented protocol. This project is not
  made, endorsed or supported by Audinate. It may break with Dante firmware updates and is not for critical shows.
- It has only been tested against itself and against the open-source Dante tools' protocol descriptions,
  **not yet against real Dante hardware**. Please report what you see.
- Our software clock is less precise than hardware. Where a Dante device exists, it takes over as clock leader automatically.
- Not supported: Dante Domain Manager, AES67-mode flows created *by* this Dante device (use the AES67 senders above), device lock, redundancy (secondary network).
- Quit Dante Virtual Soundcard and Dante Via on the same computer first, because they use the same ports. Dante Controller is fine.

## Virtual sound card (use it from Zoom, a DAW, OBS…)
- **Linux** (PulseAudio/PipeWire): creates real devices. Each send adds a speaker "AES67 &lt;name&gt;", each receive adds a mic "AES67 &lt;stream&gt;". They're removed on exit.
- **macOS:** install [BlackHole](https://existential.audio/blackhole/) 2ch and 16ch (free). They're detected automatically.
- **Windows:** install [VB-CABLE](https://vb-audio.com/Cable/) (free). Add CABLE A/B to send and receive at the same time.

## Dante setup (one time, in Dante Controller)
1. Device View → your device → **AES67 Config** → *AES67 Mode: Enabled*, then reboot the device. Use **48 kHz**.
2. From Dante into this app: **Create Multicast Flow** → tick **AES67**. The flow appears under "Streams on the network".
3. From this app into Dante: our streams appear in Dante Controller's routing grid. Subscribe to them like any channel, with a latency of ≥ 2 ms.

## Command line
```
aes67-vsc                      # tray app + control panel
aes67-vsc run [config.json]    # headless, e.g. as a service
aes67-vsc list                 # discover streams
aes67-vsc send --tone 440      # test tone
aes67-vsc receive "Stream name" -d "Speakers"
aes67-vsc setup --autostart    # (admin) firewall + permissions + start at login
aes67-vsc interfaces | devices | config
```
Config is saved per user. `aes67-vsc config` shows where.

## Limits
- 44.1/48/96 kHz, L16/L24, 1–8 channels per stream. Dante's AES67 mode is usually 48 kHz only.
- Clock sync uses software timestamps, accurate to ~10–100 µs on a LAN. That is fine for Dante's ≥1 ms latency, but not sample-accurate like hardware PTP.
- No signed native driver on macOS/Windows yet; those use the free virtual cables above.

## License
GPL-3.0-or-later (see `LICENSE`). The native Dante support is ported from
[Inferno](https://github.com/teodly/inferno) (GPL-3.0-or-later). The PTPv1 message formats follow
Inferno's [Statime fork](https://github.com/teodly/statime/tree/inferno-dev).

## Development
`pytest tests`: codec, SDP/SAP, PTP election and sync (v2 and v1), drift, multi-stream, RAVENNA, NMOS,
the Linux virtual devices, and native Dante (two virtual Dante devices routing audio to each other).
`.github/workflows/build.yml` runs the tests and builds installers for all three platforms. A tag `v*` publishes a release.
