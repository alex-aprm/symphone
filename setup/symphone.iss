; Symphone installer (Inno Setup 6). Per user, no administrator rights:
;   installs pc\dist\Symphone to %LOCALAPPDATA%\Programs\Symphone, adds a Start menu entry, starts Symphone, whose
;   setup wizard then walks through the phone app, pairing, permissions and optional extras.
; Build:  ISCC.exe /DAppVersion=2.6.0 setup\symphone.iss   ->  dist\Symphone-Setup-2.6.0.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1E2B7C-4E0B-4F0F-9C55-8E1D3A6C2F11}
AppName=Symphone
AppVersion={#AppVersion}
AppVerName=Symphone {#AppVersion}
AppPublisher=alex-aprm
AppPublisherURL=https://github.com/alex-aprm/symphone
AppSupportURL=https://github.com/alex-aprm/symphone/issues
AppUpdatesURL=https://github.com/alex-aprm/symphone/releases
DefaultDirName={localappdata}\Programs\Symphone
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Symphone-Setup-{#AppVersion}
SetupIconFile=..\pc\phonesync\ui\icon.ico
UninstallDisplayIcon={app}\Symphone.exe
UninstallDisplayName=Symphone
LicenseFile=..\LICENSE
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Files]
Source: "..\pc\dist\Symphone\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; the former name's install
Type: filesandordirs; Name: "{localappdata}\Programs\PhoneSync"

[Icons]
Name: "{userprograms}\Symphone"; Filename: "{app}\Symphone.exe"; Comment: "Your Android phone on your PC"

[Run]
Filename: "{app}\Symphone.exe"; Description: "Start Symphone"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Get-Process | Where-Object {{ $_.Path -like '{app}\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{app}\Symphone.exe"; Parameters: "--cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "Cleanup"

[Code]
// Stop a running Symphone (and the tools it started from its folder, e.g. rclone/scrcpy) so files can be replaced.
procedure StopRunning();
var
  rc: Integer;
begin
  Exec('powershell.exe', '-NoProfile -Command "Get-Process Symphone, PhoneSync -ErrorAction SilentlyContinue | Stop-Process -Force; ' +
       'Get-Process | Where-Object { $_.Path -like ''' + ExpandConstant('{localappdata}') + '\Programs\*'' -and $_.Name -in ''rclone'',''scrcpy'',''scrcpy-mic'',''adb'',''sidecar_host'' } | Stop-Process -Force"',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunning();
  Sleep(800);
  Result := '';
end;
