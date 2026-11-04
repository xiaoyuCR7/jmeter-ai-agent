@echo off
:: Puts a "JMeter Agent" shortcut on the desktop that runs start.bat, with the app's icon.
:: Run it again after moving the project folder.

set "AGENT_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = $env:AGENT_ROOT.TrimEnd('\');" ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'JMeter Agent.lnk'));" ^
  "$link.TargetPath = Join-Path $root 'start.bat';" ^
  "$link.WorkingDirectory = $root;" ^
  "$link.IconLocation = (Join-Path $root 'logo\jmeter-agent.ico') + ',0';" ^
  "$link.Description = 'Start JMeter Agent';" ^
  "$link.Save();" ^
  "Write-Host ('Shortcut created: ' + (Join-Path $desktop 'JMeter Agent.lnk'))"
if not "%1"=="/quiet" pause
