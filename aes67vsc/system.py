"""OS integration: start at login, firewall rules, privileged PTP ports."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP = "aes67-vsc"
PORTS_UDP = "319,320,5004,9875,5353"


def launch_command() -> list[str]:
    """Command that starts the tray app (frozen exe or python module)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "app", "--background"]
    return [sys.executable, "-m", "aes67vsc", "app", "--background"]


# ------------------------------------------------------------------ autostart
def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.aes67vsc.app.plist"


def _linux_desktop() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / f"{APP}.desktop"


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, APP)
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _mac_plist().exists()
    return _linux_desktop().exists()


def set_autostart(enable: bool):
    cmd = launch_command()
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except OSError:
                    pass
    elif sys.platform == "darwin":
        p = _mac_plist()
        if enable:
            args = "".join(f"<string>{a}</string>" for a in cmd)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.aes67vsc.app</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/>
</dict></plist>
""")
        elif p.exists():
            p.unlink()
    else:
        p = _linux_desktop()
        if enable:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"[Desktop Entry]\nType=Application\nName=AES67 Virtual Soundcard\n"
                         f"Exec={subprocess.list2cmdline(cmd)}\nX-GNOME-Autostart-enabled=true\n")
        elif p.exists():
            p.unlink()


# ------------------------------------------------------------------ firewall / privileges
def is_admin() -> bool:
    if sys.platform == "win32":
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False
    return os.geteuid() == 0


def firewall_commands() -> list[list[str]]:
    """Commands (run as admin) that let audio-network traffic in."""
    if sys.platform == "win32":
        prog = sys.executable
        return [["netsh", "advfirewall", "firewall", "delete", "rule", f"name={APP}"],
                ["netsh", "advfirewall", "firewall", "add", "rule", f"name={APP}", "dir=in", "action=allow",
                 f"program={prog}", "enable=yes", "profile=any", "protocol=udp"],
                ["netsh", "advfirewall", "firewall", "add", "rule", f"name={APP}", "dir=in", "action=allow",
                 f"program={prog}", "enable=yes", "profile=any", "protocol=tcp"]]
    if sys.platform.startswith("linux") and shutil.which("ufw"):
        return [["ufw", "allow", "proto", "udp", "to", "any", "port", PORTS_UDP]]
    return []  # macOS asks the user itself on first run


def ptp_ports_allowed() -> bool:
    """Can this process bind PTP ports 319/320? (Only Linux restricts them.)"""
    if not sys.platform.startswith("linux") or is_admin():
        return True
    try:
        start = int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text())
        return start <= 319
    except OSError:
        return False


def linux_privilege_fix() -> list[str]:
    """One-time command that lets PTP run without sudo on Linux."""
    target = Path(sys.executable).resolve()
    return ["setcap", "cap_net_bind_service=+ep", str(target)]


def setup(log=print) -> bool:
    """Do all one-time admin setup. Returns True if everything succeeded."""
    ok = True
    cmds = firewall_commands()
    if sys.platform.startswith("linux") and not ptp_ports_allowed():
        cmds.append(linux_privilege_fix())
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode != 0 and "delete" not in c:
                ok = False
                log(f"! {' '.join(c)}: {r.stderr.strip() or r.stdout.strip()}")
        except OSError as e:
            ok = False
            log(f"! {' '.join(c)}: {e}")
    return ok
