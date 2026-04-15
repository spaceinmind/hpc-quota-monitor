"""
HPC Disk Quota Monitor — Backend API
Parses output from the standard `quota` command as used on this cluster.

Setup:
    pip install fastapi uvicorn --user
    uvicorn server:app --host 0.0.0.0 --port 8000

Test locally first:
    curl http://localhost:8000/health
    curl http://localhost:8000/api/quota
"""

import subprocess
import shutil
import re
import os
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Configuration ──────────────────────────────────────────────────────────────
MOUNT_PATH  = os.environ.get("QUOTA_MOUNT",  "/fred/oz002")
FILESYSTEM  = os.environ.get("QUOTA_FS",     "/fred")       # as it appears in `quota` output
GROUP_NAME  = os.environ.get("QUOTA_GROUP",  "oz002")
HOME_GLOB   = os.environ.get("QUOTA_HOMES",  "/fred/oz002") # parent of per-user dirs

ALLOWED_ORIGINS = [
    "https://<YOUR-GITHUB-USERNAME>.github.io",  # ← replace with your GitHub Pages URL
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8080",
]
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="HPC Quota Monitor — oz002")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def run(cmd: str, timeout: int = 60) -> tuple[str, str]:
    """Run a shell command. Returns (stdout, stderr). Never raises."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except Exception as e:
        return "", str(e)


def parse_quota_output(output: str) -> dict | None:
    """
    Parse output from `quota -gs`.

    Handles the format seen on this cluster:
        Disk quotas for grp oz002 (gid 10199):
             Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
                  /fred 2954011876539       0 2974000000000       - 269445655       0 270000000       -
                        (Disk usage is at 99%, 2751.1 TiB of 2769.8 TiB, Inode usage is at 99%)
    """
    result = {}

    # --- Raw numbers line ---
    # Matches:   /fred  <kb_used>  <kb_soft>  <kb_hard>  <grace>  <in_used>  <in_soft>  <in_hard>
    data_re = re.compile(
        rf"^\s*{re.escape(FILESYSTEM)}\s+"
        r"(\d+)\s+"    # kbytes used
        r"(\d+)\s+"    # kbytes soft quota (0 = none)
        r"(\d+)\s+"    # kbytes hard limit
        r"\S+\s+"      # grace
        r"(\d+)\s+"    # inodes used
        r"(\d+)\s+"    # inodes soft quota
        r"(\d+)",      # inodes hard limit
        re.MULTILINE,
    )
    m = data_re.search(output)
    if m:
        kb_used  = int(m.group(1))
        kb_limit = int(m.group(3)) or int(m.group(2))   # hard limit, fall back to soft
        in_used  = int(m.group(4))
        in_limit = int(m.group(6)) or int(m.group(5))

        result["usedGB"]      = round(kb_used  / 1024 / 1024, 1)
        result["quotaGB"]     = round(kb_limit / 1024 / 1024, 1) if kb_limit > 0 else None
        result["usedInodes"]  = in_used
        result["limitInodes"] = in_limit if in_limit > 0 else None

    # --- Human-readable summary line ---
    # (Disk usage is at 99%, 2751.1 TiB of 2769.8 TiB, Inode usage is at 99%)
    summary_re = re.compile(
        r"Disk usage is at (\d+)%.*?"
        r"([\d.]+)\s*(GiB|TiB|MiB) of ([\d.]+)\s*(GiB|TiB|MiB)"
        r".*?Inode usage is at (\d+)%",
        re.IGNORECASE,
    )
    s = summary_re.search(output)
    if s:
        result["diskPct"]    = int(s.group(1))
        result["inodePct"]   = int(s.group(6))
        result["usedHuman"]  = f"{s.group(2)} {s.group(3)}"
        result["totalHuman"] = f"{s.group(4)} {s.group(5)}"

        def to_gb(val, unit):
            v = float(val)
            u = unit.lower()
            if u == "tib": return round(v * 1024, 1)
            if u == "mib": return round(v / 1024, 1)
            return round(v, 1)

        if not result.get("quotaGB"):
            result["quotaGB"] = to_gb(s.group(4), s.group(5))
        if not result.get("usedGB"):
            result["usedGB"]  = to_gb(s.group(2), s.group(3))

    return result if result else None


def get_du_users() -> list[dict]:
    """Per-user directory sizes under HOME_GLOB, sorted descending."""
    out, _ = run(
        f"du -s --block-size=1G {HOME_GLOB}/*/ 2>/dev/null | sort -rn | head -60"
    )
    users = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            gb   = int(parts[0])
            name = parts[1].rstrip("/").split("/")[-1]
            if name and gb >= 0:
                users.append({"name": name, "usedGB": gb})
        except ValueError:
            continue
    return users


def get_top_dirs() -> list[dict]:
    """Top-level subdirectory sizes under MOUNT_PATH."""
    out, _ = run(
        f"du -s --block-size=1G {MOUNT_PATH}/*/ 2>/dev/null | sort -rn | head -20"
    )
    dirs = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            dirs.append({"name": parts[1].rstrip("/"), "sizeGB": int(parts[0])})
        except ValueError:
            continue
    return dirs


@app.get("/api/quota")
def get_quota():
    """Main endpoint — returns all quota data as JSON."""

    # 1. Run quota command
    quota_out, quota_err = run("quota -gs 2>/dev/null")
    group = parse_quota_output(quota_out)

    # 2. Fallback to df if quota parsing failed
    if not group:
        df_out, _ = run(f"df -BG {MOUNT_PATH} 2>/dev/null")
        for line in df_out.splitlines():
            if MOUNT_PATH in line or FILESYSTEM in line:
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        used  = int(parts[2].rstrip("G"))
                        avail = int(parts[3].rstrip("G"))
                        total = used + avail
                        group = {
                            "usedGB":  used,
                            "quotaGB": total,
                            "diskPct": round(used / total * 100) if total else 0,
                        }
                        break
                    except ValueError:
                        pass

    if not group:
        group = {
            "usedGB": 0, "quotaGB": 0, "diskPct": None,
            "error": "quota command returned no parseable output — check /health",
        }

    # 3. Per-user and directory breakdowns (these can be slow on large filesystems;
    #    they run in the background on each request — consider caching if needed)
    users   = get_du_users()
    folders = get_top_dirs()

    return {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "mount":       MOUNT_PATH,
        "filesystem":  FILESYSTEM,
        "group":       GROUP_NAME,
        "quotaGB":     group.get("quotaGB", 0),
        "usedGB":      group.get("usedGB",  0),
        "diskPct":     group.get("diskPct"),
        "usedHuman":   group.get("usedHuman"),
        "totalHuman":  group.get("totalHuman"),
        "usedInodes":  group.get("usedInodes"),
        "limitInodes": group.get("limitInodes"),
        "inodePct":    group.get("inodePct"),
        "users":       users,
        "folders":     folders,
        "parseError":  group.get("error"),
    }


@app.get("/health")
def health():
    """
    Diagnostic endpoint. Run this first after deploying:

        curl http://localhost:8000/health

    Shows whether the path is visible, which quota tools exist,
    and the raw output of `quota -gs` for debugging.
    """
    checks = {}

    checks["path_exists"]   = os.path.exists(MOUNT_PATH)
    checks["path_readable"] = os.access(MOUNT_PATH, os.R_OK) if checks["path_exists"] else False

    checks["tools"] = {
        "quota":     shutil.which("quota")     is not None,
        "lfs":       shutil.which("lfs")       is not None,
        "beegfs-ctl":shutil.which("beegfs-ctl") is not None,
        "df":        shutil.which("df")        is not None,
        "du":        shutil.which("du")        is not None,
    }

    raw, err = run("quota -gs 2>&1")
    checks["quota_raw"] = (raw or "(no output)  stderr: " + err)[:1000]

    df_raw, _ = run(f"df -BG {MOUNT_PATH} 2>/dev/null")
    checks["df_output"] = (df_raw or "(no output)")[:400]

    parsed = parse_quota_output(raw)
    checks["parse_result"] = parsed if parsed else "FAILED — regex did not match quota output"

    all_ok = checks["path_exists"] and checks["path_readable"] and checks["tools"]["quota"]
    return {
        "status":     "ok" if all_ok else "degraded",
        "config":     {"mount": MOUNT_PATH, "filesystem": FILESYSTEM, "group": GROUP_NAME},
        "server_time": datetime.now(timezone.utc).isoformat(),
        "checks":     checks,
    }
