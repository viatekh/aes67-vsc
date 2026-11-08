# aes67-vsc — AES67 virtual soundcard

A plug-and-play network sound card for Windows, macOS and Linux. It works with:
- **Dante** devices (in AES67 mode)
- **RAVENNA** gear (Merging, Lawo, DirectOut, Neumann…)
- **NMOS** broadcast controllers
- any other AES67 device

No Audinate license is needed, because it uses the open AES67 standard.

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
- It doesn't appear as a *native* Dante device in Dante Controller. It appears through Dante's AES67 support instead.
- No signed native driver on macOS/Windows yet; those use the free virtual cables above.

## Development
`pytest tests`: codec, SDP/SAP, PTP election and sync, drift, multi-stream, RAVENNA, NMOS, and the Linux virtual devices.
`.github/workflows/build.yml` runs the tests and builds installers for all three platforms. A tag `v*` publishes a release.
