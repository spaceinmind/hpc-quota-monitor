# HPC Disk Quota Monitor

A live web dashboard for monitoring group disk quota on an HPC cluster.
The frontend is hosted on **GitHub Pages**; the backend runs on your **HPC login node**.

```
GitHub Pages (browser) ──── HTTPS GET /api/quota ────► FastAPI (login node :8000)
                                                              │
                                                    lfs quota / du / df
                                                              │
                                                         /scratch (Lustre)
```

---

## 1. Backend — HPC login node

### Install

```bash
ssh your-login-node
mkdir ~/quota-monitor && cd ~/quota-monitor
# copy backend/server.py here
pip install fastapi uvicorn --user
```

### Configure

Edit the top of `server.py`:

```python
MOUNT_PATH = "/scratch"          # your scratch filesystem
GROUP_NAME = "mygroup"           # your Unix group name
HOME_GLOB  = "/scratch/home"     # parent dir of user home dirs
QUOTA_GB   = 1000                # fallback if lfs quota fails
ALLOWED_ORIGINS = [
    "https://YOUR-USERNAME.github.io",   # ← your GitHub Pages URL
]
```

Or use environment variables instead:

```bash
export QUOTA_MOUNT=/scratch
export QUOTA_GROUP=mygroup
export QUOTA_GB=2000
```

### Run (development)

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
# Test: curl http://localhost:8000/api/quota
```

### Run (persistent with systemd)

```bash
# Copy the service file
sudo cp quota-monitor.service /etc/systemd/system/
# Edit User= and paths in the service file first, then:
sudo systemctl daemon-reload
sudo systemctl enable quota-monitor
sudo systemctl start quota-monitor
sudo systemctl status quota-monitor
```

If you don't have sudo, use `nohup` or a `screen` session:

```bash
nohup uvicorn server:app --host 0.0.0.0 --port 8000 &> quota.log &
```

### Firewall

Open port 8000 to your network (or just to your own IP for security):

```bash
# Example: ufw
sudo ufw allow from YOUR.IP.ADDRESS to any port 8000
```

---

## 2. Frontend — GitHub Pages

### Deploy

```bash
# In this repo on GitHub:
# Settings → Pages → Source: "Deploy from branch"
# Branch: main   Folder: /docs
# Save — your site will be at https://YOUR-USERNAME.github.io/REPO-NAME/
```

### Connect to your backend

1. Open the dashboard in your browser
2. Click the **Config** tab
3. Replace the API URL with:  `http://YOUR-HPC-LOGIN-NODE:8000/api/quota`
4. Click **Connect**

The URL is saved to `localStorage` — you only need to do this once per browser.

---

## 3. HTTPS / CORS note

GitHub Pages is served over **HTTPS**. Most browsers block HTTPS pages from
fetching plain **HTTP** APIs (mixed content). Two options:

**Option A — Put a reverse proxy in front (recommended)**

If your HPC site has nginx or Apache on the login node:

```nginx
# /etc/nginx/conf.d/quota.conf
server {
    listen 443 ssl;
    server_name YOUR-LOGIN-NODE;
    # ... ssl_certificate etc ...

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Then set the API URL to `https://YOUR-LOGIN-NODE/api/quota`.

**Option B — Use a tunnel (quick dev fix)**

```bash
# On your local machine:
ssh -L 8000:localhost:8000 your-login-node
```

Then point the dashboard at `http://localhost:8000/api/quota`.

---

## 4. File structure

```
hpc-quota-monitor/
├── backend/
│   ├── server.py               # FastAPI app — runs on HPC
│   └── quota-monitor.service   # systemd unit file
└── docs/
    └── index.html              # GitHub Pages frontend
```

---

## 5. Supported quota commands

The backend tries these in order:

| System | Command |
|--------|---------|
| Lustre | `lfs quota -g GROUP /scratch` |
| Generic | `df -BG /scratch` |
| Per-user | `du -s --block-size=1G /scratch/home/*` |
| Dirs | `du -s --block-size=1G /scratch/*/` |

Set `QUOTA_GB` as a fallback if neither `lfs quota` nor `df` parse correctly.
