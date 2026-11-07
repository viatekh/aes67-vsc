# aes67-vsc — basic AES67 virtual soundcard

Sends and receives network audio between your computer and **Dante** devices, using
**AES67**, the open standard Dante supports. No Audinate license needed.
Works on Windows, macOS and Linux (Python 3.9+).

## Install
```
pip install -e .
```
Linux also needs PortAudio: `sudo apt install libportaudio2`.

## Dante setup (one time, in Dante Controller)
1. Device View → your Dante device → **AES67 Config** → *AES67 Mode: Enabled*, reboot the device.
2. Set the device to **48 kHz**.
3. To send *from* Dante to this app: **Create Multicast Flow** → tick **AES67**, pick channels.
4. To receive *in* Dante from this app: streams from `aes67-vsc send` show up in the
   routing grid (shown in the device's AES67 section). Subscribe them like any Dante channel.
   Set the receiver's latency to **2 ms or more**.

## Use
```
aes67-vsc list                          # find streams on the network
aes67-vsc devices                       # list your sound cards
aes67-vsc receive "AVIO-123 : 32"       # play a Dante/AES67 stream on your speakers
aes67-vsc send --tone 440               # send a test tone
aes67-vsc send -d "Microphone" -c 2     # send a sound card input
```
Add `-i <your LAN IP>` if you have several network interfaces.

**Run `send` as admin or with sudo.** PTP clock sync uses ports 319/320, which are privileged on
Linux/macOS. Without it the stream's timestamps don't match Dante's clock, and Dante
won't play it. You don't need this for `receive`.

### Using it with other apps (like Dante Virtual Soundcard)
To make apps see it as a sound card, pair it with a free virtual audio cable:
- Windows: [VB-CABLE](https://vb-audio.com/Cable/)
- macOS: [BlackHole](https://github.com/ExistentialAudio/BlackHole)
- Linux: a PipeWire/PulseAudio null sink

Then e.g. `aes67-vsc send -d "CABLE Output"` and set your DAW/Zoom output to "CABLE Input".

## Current limits (v0.1)
- 48 kHz only, L16/L24, 1 ms packets, 1–8 channels per stream, one stream per process.
- PTP uses software timestamps with no delay measurement (accurate to ~tens of µs on a LAN).
  It follows the clock only and never acts as master, so a Dante/AES67 device must be the PTP leader.
- Clock drift between the sound card and the network is handled by dropping or padding samples, which can click occasionally.

## Tests
`pytest tests` covers codec, SDP/SAP/PTP parsing, and a full send → discover → receive loopback.
