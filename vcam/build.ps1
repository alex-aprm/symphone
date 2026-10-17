# Builds PhoneSyncVCam.dll + PhoneSyncCam.exe (Release x64) into vcam\bin
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$bin  = Join-Path $root 'bin'
$obj  = Join-Path $root 'obj'
New-Item -ItemType Directory -Force $bin, $obj | Out-Null

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsPath = $null
if (Test-Path $vswhere) { $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath }
if (-not $vsPath) { $vsPath = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools" }
$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found under $vsPath" }

$cflags = '/nologo /c /O2 /MT /EHsc /std:c++17 /W3 /DUNICODE /D_UNICODE /DNDEBUG /Zi'
$dllSrc = 'log.cpp shm.cpp stream.cpp source.cpp dllmain.cpp'
$dllLibs = 'mfplat.lib mf.lib mfuuid.lib mfsensorgroup.lib ole32.lib oleaut32.lib advapi32.lib propsys.lib strmiids.lib'
$exeLibs = 'mfplat.lib mf.lib mfuuid.lib mfsensorgroup.lib mfreadwrite.lib ole32.lib oleaut32.lib advapi32.lib propsys.lib shell32.lib'

$script = @"
@echo off
call "$vcvars" >nul 2>&1 || exit /b 1
cd /d "$root\src" || exit /b 1
cl $cflags /Fo"$obj\\" /Fd"$obj\dll.pdb" $dllSrc || exit /b 1
link /nologo /DLL /DEBUG /OPT:REF /OPT:ICF /DEF:PhoneSyncVCam.def /OUT:"$bin\PhoneSyncVCam.dll" /PDB:"$bin\PhoneSyncVCam.pdb" "$obj\log.obj" "$obj\shm.obj" "$obj\stream.obj" "$obj\source.obj" "$obj\dllmain.obj" $dllLibs || exit /b 1
cd /d "$root\host" || exit /b 1
cl $cflags /I"$root\src" /Fo"$obj\\" /Fd"$obj\exe.pdb" PhoneSyncCam.cpp || exit /b 1
link /nologo /DEBUG /OPT:REF /OPT:ICF /SUBSYSTEM:CONSOLE /OUT:"$bin\PhoneSyncCam.exe" /PDB:"$bin\PhoneSyncCam.pdb" "$obj\PhoneSyncCam.obj" $exeLibs || exit /b 1
"@
$bat = Join-Path $obj 'build.cmd'
Set-Content -Path $bat -Value $script -Encoding ASCII
$ErrorActionPreference = 'Continue'
& cmd.exe /c "`"$bat`" 2>&1" | ForEach-Object { Write-Host "$_" }
$ErrorActionPreference = 'Stop'
if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }
Write-Host "Build OK -> $bin"
Get-ChildItem $bin -Include *.dll, *.exe -Recurse | Format-Table Name, Length, LastWriteTime


