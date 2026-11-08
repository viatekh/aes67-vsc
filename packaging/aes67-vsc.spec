# PyInstaller spec: one-folder build with the tray app as default.
import sys
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(["entry.py"], pathex=[".."], datas=[("../aes67vsc/ui.html", "aes67vsc")],
             hiddenimports=collect_submodules("zeroconf") + collect_submodules("pystray"))
pyz = PYZ(a.pure)
console = sys.platform.startswith("linux")  # Windows/macOS: no console window, tray only
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="aes67-vsc", console=console)
coll = COLLECT(exe, a.binaries, a.datas, name="aes67-vsc")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="AES67 Virtual Soundcard.app", bundle_identifier="com.aes67vsc.app",
                 info_plist={"LSUIElement": True,  # menu-bar app, no dock icon
                             "NSMicrophoneUsageDescription": "Send your audio inputs to the network.",
                             "NSLocalNetworkUsageDescription": "Send and receive audio on the local network."})
