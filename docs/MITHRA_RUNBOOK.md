# Mithra Life OS — Operations Runbook

---

## What We Built

### Architecture
```
Mac (your dev machine)
    │
    │  ssh win  (port 2222, key auth, no password)
    ▼
Windows Laptop (192.168.68.92)
    │  Windows OpenSSH on port 22
    │  Port Proxy: 2222 → WSL Ubuntu 2222
    ▼
Ubuntu WSL2 (Docker Engine)
    │  systemd manages: docker, ssh, wsl-keepalive, mithra
    ▼
Mithra Stack (docker compose)
    │  api-gateway      → port 9000
    │  llm-router       → port 4000
    │  vector-store     → port 8001
    │  cloudflared      → Cloudflare tunnel
    ▼
assistant.mithra-app.com (public endpoint)
```

### What Changed Today
| Before | After |
|---|---|
| Docker Desktop (Electron, unstable) | Docker Engine in WSL2 (stable, lightweight) |
| 2 GB free on C: | 49 GB free on C: |
| 42 GB docker_data.vhdx | Compacted + migrated to new engine |
| Manual docker commands | `mithra` CLI command |
| No SSH from Mac | `ssh win` — instant, no password |
| No auto-start | systemd + Task Scheduler on boot |

### Key File Locations
| What | Where |
|---|---|
| docker-compose.yml | `~/mithra/docker-compose.yml` (Ubuntu) |
| .env file | `~/mithra/.env` (Ubuntu) |
| Mithra data volume | Docker volume `mithra_mithra-data` |
| Postgres volume | Docker volume `ai-home-assistant_postgres_data` |
| Backups | `C:\mithra\backups\` (Windows) |
| WSL config | `C:\Users\abhi_\.wslconfig` |
| Wake script | `C:\Users\abhi_\wake-wsl.vbs` |

---

## Day-to-Day Workflow

### Making a Code Change
```
1. Write code on Mac
        ↓
2. Push to GitHub → Actions builds new Docker image automatically
        ↓
3. SSH into server:
   ssh win
        ↓
4. Deploy:
   mithra update
        ↓
5. Verify:
   mithra status
```

### Mithra Commands (run from ssh win)
```bash
mithra start          # start all containers
mithra stop           # stop all containers
mithra restart        # restart without pulling new images
mithra update         # pull latest images + restart (USE FOR DEPLOYS)
mithra status         # show container health + disk usage
mithra logs           # tail all logs (Ctrl+C to exit)
mithra logs api-gateway    # tail specific service logs
mithra cleanup        # remove dangling images + build cache (run monthly)
```

### SSH Access
```bash
ssh win               # from Mac — instant, no password
ssh abhi_@192.168.68.92        # fallback: Windows PowerShell
ssh abhiram@192.168.68.92 -p 2222  # fallback: Ubuntu direct
```

---

## Boot Sequence (Automatic)

When Windows starts:
```
1. Windows boots + user logs in
2. Task Scheduler runs "WSL Keepalive" → starts wsl -d Ubuntu -- sleep infinity
3. systemd starts inside Ubuntu:
   ├── docker.service        (Docker Engine)
   ├── ssh.service           (SSH server on port 2222)
   ├── wsl-keepalive.service (keeps WSL alive)
   └── mithra.service        (docker compose up -d)
4. Port proxy updated: Windows:2222 → Ubuntu:2222
5. Cloudflare tunnel reconnects → assistant.mithra-app.com is live
```
**You don't need to do anything after a reboot — everything comes back automatically.**

---

## Troubleshooting

### Can't SSH into server (`ssh win` fails)

**Step 1 — Check if Windows is reachable:**
```bash
ping 192.168.68.92
```

**Step 2 — SSH into Windows directly:**
```bash
ssh abhi_@192.168.68.92
```

**Step 3 — Check WSL state:**
```powershell
wsl --list --verbose
```

**If Ubuntu shows Stopped — restart everything:**
```powershell
# Trigger the keepalive task
schtasks /run /tn "WSL Keepalive"
Start-Sleep 15
wsl --list --verbose

# Update port proxy
$wslIp = wsl -d Ubuntu -e hostname -I | ForEach-Object { $_.Trim().Split(' ')[0] }
netsh interface portproxy delete v4tov4 listenport=2222 listenaddress=0.0.0.0
netsh interface portproxy add v4tov4 listenport=2222 listenaddress=0.0.0.0 connectport=2222 connectaddress=$wslIp
Write-Host "WSL IP: $wslIp"
```

**If Ubuntu shows Running but SSH still fails:**
```powershell
wsl -d Ubuntu -- sudo systemctl restart ssh
```

---

### Mithra containers not running

```bash
ssh win
mithra status         # check what's up/down
mithra start          # start everything
mithra logs           # check for errors
```

If a specific container is unhealthy:
```bash
mithra logs api-gateway      # replace with the unhealthy service name
mithra restart               # often fixes transient issues
```

---

### `assistant.mithra-app.com` not responding

```bash
ssh win
mithra status
```

Check if `mithra-cloudflared-1` is running. If not:
```bash
docker compose -f ~/mithra/docker-compose.yml up -d cloudflared
```

If cloudflared is running but tunnel is down, check logs:
```bash
mithra logs cloudflared
```

---

### Running out of disk space

```bash
ssh win
mithra status         # shows disk usage
mithra cleanup        # removes dangling images + cache
```

If C: drive is still low (check from Windows):
```powershell
Get-PSDrive C | Select-Object @{N="Free_GB";E={[math]::Round($_.Free/1GB,2)}}
```

Run the monthly compact script (causes ~10 min downtime):
```powershell
# From Windows PowerShell as Administrator
# Stop everything
wsl --shutdown
# Run diskpart compact (not needed anymore — Docker moved to WSL Ubuntu's own disk)
```

---

### Docker Engine not responding

```bash
ssh win
sudo systemctl status docker
sudo systemctl restart docker
mithra start
```

---

### WSL keeps crashing

Check the keepalive task is running:
```powershell
# From Windows PowerShell
Get-ScheduledTask -TaskName "WSL Keepalive" | Select-Object TaskName, State
schtasks /run /tn "WSL Keepalive"
```

Check systemd services inside Ubuntu:
```bash
ssh win
systemctl status wsl-keepalive docker ssh mithra
```

Re-enable anything that's disabled:
```bash
sudo systemctl enable wsl-keepalive docker ssh mithra
sudo systemctl start wsl-keepalive docker ssh mithra
```

---

## When Your Router Restarts

Your Windows machine's LAN IP (`192.168.68.92`) might change after a router restart if DHCP reassigns it.

### Check the new IP (from Windows):
```powershell
ipconfig | findstr "IPv4"
```

### Update Mac SSH config if IP changed:
```bash
nano ~/.ssh/config
# Update the HostName under Host win
```

### Permanent fix — assign a static IP to your Windows machine:
In your router admin panel (usually `192.168.68.1`):
- Find DHCP reservations / static leases
- Bind your Windows laptop's MAC address to `192.168.68.92`
- This prevents the IP from ever changing

To find your Windows MAC address:
```powershell
ipconfig /all | findstr "Physical Address"
```

---

## Watchtower Auto-Updates

Watchtower automatically pulls new images and restarts containers when you push to GitHub. 

**Add `--cleanup` flag** to prevent old images accumulating (add to docker-compose.yml):
```yaml
watchtower:
  image: containrrr/watchtower:latest
  command: --interval 86400 --cleanup
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
  restart: unless-stopped
```

Then apply:
```bash
ssh win
cd ~/mithra
docker compose up -d watchtower
```

---

## Monthly Maintenance Checklist

```bash
ssh win

# 1. Check disk usage
mithra status

# 2. Clean up Docker artifacts
mithra cleanup

# 3. Check all services are healthy
systemctl status wsl-keepalive docker ssh mithra

# 4. Pull latest images manually if Watchtower missed anything
mithra update

# 5. Check C: drive free space (from Windows)
# Should stay above 20 GB
```

---

## Emergency: Full Stack Recovery

If everything is broken after a bad update or crash:

```bash
ssh abhi_@192.168.68.92     # Windows SSH fallback
```

```powershell
# In Windows PowerShell
schtasks /run /tn "WSL Keepalive"
Start-Sleep 15
$wslIp = wsl -d Ubuntu -e hostname -I | ForEach-Object { $_.Trim().Split(' ')[0] }
netsh interface portproxy delete v4tov4 listenport=2222 listenaddress=0.0.0.0
netsh interface portproxy add v4tov4 listenport=2222 listenaddress=0.0.0.0 connectport=2222 connectaddress=$wslIp
wsl -d Ubuntu
```

```bash
# In Ubuntu
sudo systemctl start docker
sudo systemctl start ssh
cd ~/mithra
git pull origin main          # get latest compose file
mithra start                  # bring stack up
mithra status                 # verify
```

If containers won't start due to bad images, roll back:
```bash
cd ~/mithra
docker compose pull           # pull fresh images
mithra start
```

---

## Key Credentials & Config

| Item | Value |
|---|---|
| Windows LAN IP | 192.168.68.92 |
| SSH alias | `ssh win` |
| Ubuntu user | abhiram |
| SSH port | 2222 |
| Mithra API | http://192.168.68.92:9000 |
| Public endpoint | https://assistant.mithra-app.com |
| Household ID | hh_b76fef08 |
| Timezone | America/Chicago |
| GitHub repo | github.com/agaddampalli/mithra |

---

*Last updated: June 2026 — after migration from Docker Desktop to Docker Engine in WSL2*
