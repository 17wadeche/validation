[Setup]
AppName=Validation UI Launcher
AppVersion=1.0.0
DefaultDirName={localappdata}\Validation_v1
DefaultGroupName=Validation
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputBaseFilename=ValidationLauncherSetup
Compression=lzma
SolidCompression=yes

[Files]
Source: "C:\validation\dist\ValidationLauncher.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Validation UI"; Filename: "{app}\ValidationLauncher.exe"
