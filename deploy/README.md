# Deployment Automation

This folder contains fully automated install/uninstall scripts for both Linux and Windows.

## Layout
- `deploy/linux/install.sh`
- `deploy/linux/uninstall.sh`
- `deploy/windows/install.ps1`
- `deploy/windows/uninstall.ps1`
- `deploy/windows/start-web.ps1`
- `deploy/windows/start-genesys-connector.ps1`
- `deploy/windows/start-audiohook.ps1`

## What The Installers Do
- Create `.venv` and install Python dependencies from `requirements.txt`
- Create `.env` from `.env.example` if `.env` does not exist
- Create required folders (`data/`, `log/`, runtime subfolders)
- Run `python manage.py migrate`
- Run `python manage.py check`
- Register startup services/tasks (unless disabled)

## Linux
### Install
```bash
cd /path/to/call_analytics
chmod +x deploy/linux/install.sh deploy/linux/uninstall.sh
sudo ./deploy/linux/install.sh
```

Custom install example:
```bash
sudo ./deploy/linux/install.sh --web-port 8009 --python python3 --app-user deploy --app-group deploy
```

Without systemd services:
```bash
./deploy/linux/install.sh --no-systemd
```

### Services Installed (systemd mode)
- `call-analytics-web.service`
- `call-analytics-genesys-connector.service`
- `call-analytics-genesys-audiohook.service`

Status checks:
```bash
systemctl status call-analytics-web.service
systemctl status call-analytics-genesys-connector.service
systemctl status call-analytics-genesys-audiohook.service
```

### Uninstall
```bash
sudo ./deploy/linux/uninstall.sh
```

Optional full cleanup:
```bash
sudo ./deploy/linux/uninstall.sh --purge-venv --purge-data --purge-logs --purge-env
```

## Windows
### Install
```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\install.ps1
```

Custom install example:
```powershell
.\deploy\windows\install.ps1 -WebPort 8009 -PythonCommand python
```

Skip startup tasks:
```powershell
.\deploy\windows\install.ps1 -SkipScheduledTasks
```

### Scheduled Tasks Installed
- `CallAnalytics-Web`
- `CallAnalytics-GenesysConnector`
- `CallAnalytics-GenesysAudioHook`

Task checks:
```powershell
Get-ScheduledTask -TaskName "CallAnalytics-*"
```

### Uninstall
```powershell
.\deploy\windows\uninstall.ps1
```

Optional full cleanup:
```powershell
.\deploy\windows\uninstall.ps1 -PurgeVenv -PurgeData -PurgeLogs -PurgeEnv
```

## Post-Install
1. Edit `.env` with your provider credentials and endpoints.
2. Verify app health:
   - `http://127.0.0.1:8009/`
   - `http://127.0.0.1:8009/api/integrations/genesys/health`
   - `http://127.0.0.1:8009/api/integrations/genesys/audiohook/health`
3. If using Genesys AudioHook, expose websocket path via HTTPS/WSS reverse proxy:
   - default path: `/audiohook/ws`
