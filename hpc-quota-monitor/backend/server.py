import subprocess
import shutil
import re
import os
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

MOUNT_PATH  = "/fred/oz002"
FILESYSTEM  = "/fred"
GROUP_NAME  = "oz002"
HOME_GLOB   = "/fred/oz002"

ALLOWED_ORIGINS = [
    "https://<YOUR-GITHUB-USERNAME>.github.io",
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app = FastAPI(title="HPC Quota Monitor")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET"], allow_headers=["*"])

def run(cmd: str, timeout: int = 60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr
    except Exception as e:
        return "", str(e)

def parse_quota_output(output: str) -> Optional[dict]:
    result = {}
    data_re = re.compile(
        rf"^\s*{re.escape(FILESYSTEM)}\s+"
        r"(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+(\d+)\s+(\d+)\s+(\d+)",
        re.MULTILINE,
    )
    m = data_re.search(output)
    if m:
        kb_used  = int(m.group(1))
        kb_limit = int(m.group(3)) or int(m.group(2))
        in_used  = int(m.group(4))
        in_limit = int(m.group(6)) or int(m.group(5))
        result["usedGB"]      = round(kb_used  / 1024 / 1024, 1)
        result["quotaGB"]     = round(kb_limit / 1024 / 1024, 1) if kb_limit > 0 else None
        result["usedInodes"]  = in_used
        result["limitInodes"] = in_limit if in_limit > 0 else None
    summary_re = re.compile(
        r"Disk usage is at (\d+)%.*?([\d.]+)\s*(GiB|TiB|MiB) of ([\d.]+)\s*(GiB|TiB|MiB)"
        r".*?Inode usage is at (\d+)%", re.IGNORECASE,
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

def get_du_users() -> List[dict]:
    out, _ = run(f"du -s --block-size=1G {HOME_GLOB}/*/ 2>/dev/null | sort -rn | head -60")
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

def get_top_dirs() -> List[dict]:
    out, _ = run(f"du -s --block-size=1G {MOUNT_PATH}/*/ 2>/dev/null | sort -rn | head -20")
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
    quota_out, _ = run("quota -gs 2>/dev/null")
    group = parse_quota_output(quota_out)
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
                        group = {"usedGB": used, "quotaGB": total,
                                 "diskPct": round(used / total * 100) if total else 0}
                        break
                    except ValueError:
                        pass
    if not group:
        group = {"usedGB": 0, "quotaGB": 0, "diskPct": None, "error": "quota returned no output"}
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
    checks = {}
    checks["path_exists"]   = os.path.exists(MOUNT_PATH)
    checks["path_readable"] = os.access(MOUNT_PATH, os.R_OK) if checks["path_exists"] else False
    checks["tools"] = {
        "quota": shutil.which("quota") is not None,
        "df":    shutil.which("df")    is not None,
        "du":    shutil.which("du")    is not None,
    }
    raw, err = run("quota -gs 2>&1")
    checks["quota_raw"]    = (raw or "(no output) stderr: " + err)[:1000]
    parsed = parse_quota_output(raw)
    checks["parse_result"] = parsed if parsed else "FAILED — regex did not match"
    all_ok = checks["path_exists"] and checks["path_readable"] and checks["tools"]["quota"]
    return {
        "status":      "ok" if all_ok else "degraded",
        "config":      {"mount": MOUNT_PATH, "filesystem": FILESYSTEM, "group": GROUP_NAME},
        "server_time": datetime.now(timezone.utc).isoformat(),
        "checks":      checks,
    }
