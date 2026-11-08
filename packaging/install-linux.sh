#!/bin/sh
# Installs to /opt/aes67-vsc, lets PTP run without root, adds menu entry + autostart.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
sudo rm -rf /opt/aes67-vsc && sudo cp -r "$DIR/aes67-vsc" /opt/aes67-vsc
sudo ln -sf /opt/aes67-vsc/aes67-vsc /usr/local/bin/aes67-vsc
sudo setcap cap_net_bind_service=+ep /opt/aes67-vsc/aes67-vsc   # PTP ports 319/320
if command -v ufw >/dev/null && sudo ufw status | grep -q active; then
  sudo ufw allow proto udp to any port 319,320,5004,9875,5353
fi
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/aes67-vsc.desktop <<D
[Desktop Entry]
Type=Application
Name=AES67 Virtual Soundcard
Exec=/opt/aes67-vsc/aes67-vsc app
Categories=AudioVideo;Audio;
D
/opt/aes67-vsc/aes67-vsc app --background >/dev/null 2>&1 &
echo "Installed. Control panel: http://127.0.0.1:8067/"
