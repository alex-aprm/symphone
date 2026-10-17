@echo off
rem Builds the sidecar native helpers into ..\bin (requires Visual Studio 2022 / Build Tools with C++ and the Windows SDK).
setlocal
set VSWHERE="%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq delims=" %%i in (`%VSWHERE% -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VSDIR=%%i
if not defined VSDIR set VSDIR=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cd /d "%~dp0"
if not exist ..\bin mkdir ..\bin
if not exist obj mkdir obj
cl /nologo /O2 /EHsc /std:c++20 /MT /W3 /DUNICODE /D_UNICODE /Foobj\ vddctl.cpp /Fe..\bin\vddctl.exe /link /SUBSYSTEM:CONSOLE || exit /b 1
if exist sidecar_host.cpp (
  cl /nologo /O2 /EHsc /std:c++20 /MT /W3 /await:strict /DUNICODE /D_UNICODE /Foobj\ sidecar_host.cpp /Fe..\bin\sidecar_host.exe /link /SUBSYSTEM:CONSOLE || exit /b 1
)
echo OK
