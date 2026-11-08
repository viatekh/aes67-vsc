; Inno Setup script: installs, opens firewall, starts at login, launches.
#define AppVer GetEnv("APP_VERSION")
[Setup]
AppName=AES67 Virtual Soundcard
AppVersion={#AppVer}
DefaultDirName={autopf}\AES67 Virtual Soundcard
DefaultGroupName=AES67 Virtual Soundcard
OutputBaseFilename=aes67-vsc-setup
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes

[Files]
Source: "..\dist\aes67-vsc\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\AES67 Virtual Soundcard"; Filename: "{app}\aes67-vsc.exe"; Parameters: "app"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "aes67-vsc"; ValueData: """{app}\aes67-vsc.exe"" app --background"; Flags: uninsdeletevalue; Tasks: autostart

[Tasks]
Name: autostart; Description: "Start automatically at login"

[Run]
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=aes67-vsc dir=in action=allow program=""{app}\aes67-vsc.exe"" enable=yes profile=any protocol=udp"; Flags: runhidden
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=aes67-vsc dir=in action=allow program=""{app}\aes67-vsc.exe"" enable=yes profile=any protocol=tcp"; Flags: runhidden
Filename: "{app}\aes67-vsc.exe"; Parameters: "app"; Flags: nowait postinstall skipifsilent; Description: "Launch now"

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=aes67-vsc"; Flags: runhidden; RunOnceId: "fw"
