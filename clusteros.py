#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════════
#   ClusterOS  —  Unified Cluster Management Suite
#   Monitor · Transfer · Settings  in one Mission-Control interface
#
#   FEATURES
#     • Light / Dark theme toggle (live)
#     • Responsive tile grid for the monitor (reflows to window width)
#     • Settings panel (refresh, workers, retries, alert thresholds, data source)
#     • Throttle-safe transfers: pooled / reused SSH connections + retry-backoff
#     • Transparent jump-host tunneling for nodes on an internal LAN
#     • Slurm queue / core-usage detail, remote file browser, remote desktop
#
#   INSTALL:  see README.md, or run  ./setup.sh
#     pip install -e .
#     extended-display placement (optional): pip install "clusteros[display]"
#
#   INVENTORY:  clusters.xlsx  → hostname | ip | username | password | key | via
#     Copy clusters.example.xlsx to clusters.xlsx and fill in your own nodes.
# ═══════════════════════════════════════════════════════════════════════════════

import os, sys, json, stat, time, socket, shutil, platform, threading, subprocess
from collections        import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime           import datetime
from pathlib            import Path
import tkinter as tk
from tkinter import filedialog, messagebox

for pkg in ("customtkinter", "paramiko", "openpyxl"):
    try: __import__(pkg)
    except ImportError:
        sys.exit(f"Missing package — run:  pip install {pkg}"
                 f"   (or install everything with:  pip install -e .)")

import customtkinter as ctk
from paramiko import SSHClient, AutoAddPolicy
import openpyxl

OSNAME = platform.system().lower()

# ═══════════════════════════════════════════════════════════════════════════════
#   PERSISTENT CONFIG   (~/.clusteros/config.json)
# ═══════════════════════════════════════════════════════════════════════════════
CFG_DIR  = Path.home() / ".clusteros"
CFG_FILE = CFG_DIR / "config.json"

DEFAULTS = {
    "theme":           "dark",     # "dark" | "light"
    "refresh_time":    10,         # monitor poll interval (s)
    "monitor_workers": 8,          # parallel probes
    "sftp_workers":    3,          # parallel SFTP streams (also caps handshakes)
    "connect_retries": 4,          # SSH connect attempts before giving up
    "banner_timeout":  30,         # seconds to wait for SSH banner
    "ssh_timeout":     5,          # base connect/command timeout
    "cpu_alert":       90.0,
    "ram_alert":       90.0,
    "excel_path":      "",         # optional override path to clusters.xlsx
    "notifications":   False,      # desktop alert pop-ups (OFF = silent)
    "gateway":         "",         # hostname of a reachable node to tunnel through
                                   #   to reach nodes your network doesn't route
                                   #   to directly. Blank = auto-detect.
    "auto_gateway":    True,       # if a node isn't directly reachable, tunnel via
                                   #   the best-matching reachable node automatically
    "sim_process_pattern": "",     # extended-regex matching your own long-running
                                   #   job processes, counted per node alongside
                                   #   Slurm jobs. e.g. "my-solver|run\\.sh".
                                   #   Blank = count Slurm jobs only.
    "x11_forward":     True,       # add `ssh -X` when opening an interactive
                                   #   terminal, so remote GUI apps display locally
                                   #   (needs a local X server: XQuartz / X11)
}
CFG = dict(DEFAULTS)

def load_config():
    global CFG
    try:
        if CFG_FILE.exists():
            data = json.loads(CFG_FILE.read_text())
            CFG.update({k: data[k] for k in DEFAULTS if k in data})
    except Exception:
        pass

def save_config():
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        CFG_FILE.write_text(json.dumps(CFG, indent=2))
    except Exception as e:
        print(f"[config] save failed: {e}", file=sys.stderr)

load_config()

SPARK_LEN    = 30
BACKOFF_BASE = 1.5          # seconds; doubles each retry, capped at 12

# ═══════════════════════════════════════════════════════════════════════════════
#   THEMES  — two palettes with identical keys; T is the live one
# ═══════════════════════════════════════════════════════════════════════════════
DARK = {
    "void":"#05070d","bg":"#080c14","panel":"#0b1018","card":"#0f1520",
    "card2":"#131c2a","raised":"#182030","border":"#1c2838","border2":"#243248",
    "cyan":"#00e5ff","cyanh":"#00b8d4","cyanl":"#80f0ff","green":"#00ff9d",
    "greenh":"#00cc7d","amber":"#ffb300","red":"#ff3d57","violet":"#b040ff",
    "text":"#dde8f8","muted":"#7e91b0","dim":"#3a4d68","track":"#111922",
}
LIGHT = {
    "void":"#dbe2ee","bg":"#f3f5fa","panel":"#ffffff","card":"#ffffff",
    "card2":"#eef2f8","raised":"#f1f4fa","border":"#e0e6f0","border2":"#cdd6e6",
    "cyan":"#0094b8","cyanh":"#007a99","cyanl":"#2bb6d4","green":"#00a86b",
    "greenh":"#00875a","amber":"#c47d00","red":"#e23854","violet":"#8a3fd1",
    "text":"#1a2330","muted":"#5f6e85","dim":"#9fadc2","track":"#e6ebf3",
}
THEMES = {"dark": DARK, "light": LIGHT}
T = dict(THEMES.get(CFG["theme"], DARK))     # live palette (mutated in place)

def apply_theme(name):
    """Swap the live palette in place so every T[...] lookup updates."""
    T.clear(); T.update(THEMES.get(name, DARK))
    CFG["theme"] = name
    ctk.set_appearance_mode("light" if name == "light" else "dark")

ctk.set_appearance_mode("light" if CFG["theme"] == "light" else "dark")
ctk.set_default_color_theme("blue")

MONO = ("Consolas"      if OSNAME == "windows" else
        "JetBrains Mono" if OSNAME == "darwin"  else "DejaVu Sans Mono")
SANS = ("Segoe UI"      if OSNAME == "windows" else
        "SF Pro Display" if OSNAME == "darwin"  else "Ubuntu")

def F(sz=13, bold=False):
    return ctk.CTkFont(family=SANS, size=sz, weight="bold" if bold else "normal")
def FM(sz=11, bold=False):
    return ctk.CTkFont(family=MONO, size=sz, weight="bold" if bold else "normal")

def lvl_color(pct):
    if pct is None:  return T["dim"]
    if pct >= 90:    return T["red"]
    if pct >= 70:    return T["amber"]
    return T["green"]

def to_float(v):
    try: return float(str(v).strip())
    except: return None

def fmt_uptime(secs):
    try: secs = int(float(secs))
    except: return "—"
    d,r = divmod(secs,86400); h,r = divmod(r,3600); m,_ = divmod(r,60)
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    return f"{m}m"

def human_size(n):
    try: n = float(n)
    except: return "0 B"
    for u in ("B","KB","MB","GB","TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def notify(title, msg):
    # Honour the user setting — when notifications are off, stay silent.
    if not CFG.get("notifications", False):
        return
    try:
        if OSNAME == "darwin":
            subprocess.Popen(["osascript","-e",
                f'display notification "{msg}" with title "{title}"'])
        elif OSNAME == "linux" and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, msg])
    except: pass


# ═══════════════════════════════════════════════════════════════════════════════
#   DISPLAY PLACEMENT
# ═══════════════════════════════════════════════════════════════════════════════
WIN_W, WIN_H = 1380, 860

def _enumerate_monitors():
    try:
        from screeninfo import get_monitors
        return [(int(m.x),int(m.y),int(m.width),int(m.height),
                 bool(getattr(m,"is_primary",False))) for m in get_monitors()]
    except Exception:
        return []

def pick_window_geometry(tk_root, win_w=WIN_W, win_h=WIN_H):
    mons = _enumerate_monitors()
    if not mons: return f"{win_w}x{win_h}"
    primary = next((m for m in mons if m[4]), None) \
        or next((m for m in mons if m[0]==0 and m[1]==0), mons[0])
    extended = next((m for m in mons if m is not primary), None)
    target = extended or primary
    scale = 1.0
    try:
        if primary[2]:
            scale = tk_root.winfo_screenwidth() / primary[2]
    except Exception:
        pass
    if not (0.3 < scale < 3.0): scale = 1.0
    mx,my,mw,mh = (target[0]*scale, target[1]*scale, target[2]*scale, target[3]*scale)
    w = min(win_w, int(mw)); h = min(win_h, int(mh))
    x = int(mx + (mw-w)/2);  y = int(my + (mh-h)/2)
    return f"{w}x{h}+{x}+{y}"


# ═══════════════════════════════════════════════════════════════════════════════
#   EXCEL LOADER
# ═══════════════════════════════════════════════════════════════════════════════
def _excel_candidates():
    cands = []
    if CFG.get("excel_path"): cands.append(Path(CFG["excel_path"]).expanduser())
    cands += [Path(__file__).parent / "clusters.xlsx",
              Path.cwd() / "clusters.xlsx",
              Path.home() / "clusters.xlsx"]
    return cands

def load_clusters_xlsx():
    path = next((p for p in _excel_candidates() if p.exists()), None)
    if not path: return []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows: return []
        hdr = [str(c).strip().lower() if c else "" for c in rows[0]]
        col = {n:i for i,n in enumerate(hdr)}
        nc = col.get("hostname") or col.get("name")    or 0
        ic = col.get("ip")       or col.get("address") or 1
        uc = col.get("username") or col.get("user")    or 2
        pc = col.get("password") or col.get("pass")    or 3
        kc = col.get("key")      or col.get("keyfile") or None
        vc = col.get("via")      or col.get("jump") or col.get("gateway") or None
        # host_dir = folder on the REMOTE machine that contains host.py (for the
        # Remote Desktop feature). Accepts a few header spellings.
        dc = (col.get("host_dir") or col.get("host folder") or col.get("host_folder")
              or col.get("hostpath") or col.get("host_path") or col.get("desktop_dir")
              or col.get("rd_dir") or None)
        def cell(row, idx):
            if idx is None or idx >= len(row): return ""
            return str(row[idx]).strip() if row[idx] is not None else ""
        return [{"hostname":cell(r,nc),"ip":cell(r,ic),
                 "username":cell(r,uc),"password":cell(r,pc),
                 "key":cell(r,kc),"name":cell(r,nc),"via":cell(r,vc),
                 "host_dir":cell(r,dc)}
                for r in rows[1:] if any(r)]
    except Exception as e:
        messagebox.showerror("Excel error", str(e)); return []


# ═══════════════════════════════════════════════════════════════════════════════
#   SLURM HOSTS
#   ---------------------------------------------------------------------------
#   This dict is now only an OPTIONAL OVERRIDE.  Slurm is auto-detected on every
#   host at probe time (the remote probe reports whether `sinfo`/`squeue` exist),
#   so any Slurm-configured cluster lights up its "Slurm detail" automatically
#   without being listed here.
#
#   Add a host here ONLY when you need to pin a value the probe can't infer:
#       • partition  : restrict the view to one partition  (""  = all)
#       • user       : whose jobs to show          (default = the SSH username)
#       • core_limit : the per-user CPU-CORE cap    (0 = auto-detect / none)
#
#   IMPORTANT:  core_limit is a CORE limit, not a node limit.  On a cluster whose
#   nodes have N cores each, a cap of 64 means 64 CPU cores, not 64 nodes.
#
#   Example:
#       SLURM_HOSTS = {
#           "my-cluster": {"partition": "gpu", "user": "alice", "core_limit": 64},
#       }
# ═══════════════════════════════════════════════════════════════════════════════
SLURM_HOSTS = {}

def slurm_cfg_for(host, row):
    """Build the Slurm query config for a host: explicit overrides win, otherwise
    sensible defaults so auto-detected hosts work with zero configuration."""
    cfg = dict(SLURM_HOSTS.get(host, {}))
    cfg.setdefault("partition", "")
    cfg.setdefault("user", row.get("username", ""))
    # Back-compat: the old "node_limit" key actually held a CORE count.
    if "core_limit" not in cfg:
        cfg["core_limit"] = cfg.get("node_limit", 0)
    return cfg

# ═══════════════════════════════════════════════════════════════════════════════
#   GATEWAY / JUMP-HOST ROUTING  — reach nodes your network doesn't route to
#   ---------------------------------------------------------------------------
#   In many deployments only a couple of nodes are directly routable from where
#   you sit. The rest live on an internal LAN that one of those reachable nodes
#   CAN see. We tunnel SSH / SFTP to the unreachable nodes THROUGH a reachable
#   one (the "gateway") over an SSH direct-tcpip channel.
#
#   This is a TCP-level relay, NOT a copy: bytes pass through the gateway's
#   network stack and land DIRECTLY on the target's disk. Nothing is ever
#   written to the gateway — no staging file, no double copy. It is exactly
#   `scp -J` / `ProxyJump`, done in-process.
#
#   Which node is used as the jump, in priority order:
#     1. per-node `via` column in clusters.xlsx   (via | jump | gateway)
#     2. global CFG["gateway"]  (a hostname)
#     3. auto: if a node isn't directly reachable and CFG["auto_gateway"] is on,
#        pick the cluster whose IP shares the longest prefix with it, preferring
#        one that is itself directly reachable right now.
# ═══════════════════════════════════════════════════════════════════════════════
import shlex
try:
    from paramiko.ssh_exception import ChannelException
except Exception:
    ChannelException = Exception

CLUSTER_INDEX = {}            # hostname -> row  (populated by register_clusters)
_GW_POOLS     = {}            # gateway-hostname -> _GatewayLink (one live SSH each)
_GW_POOLS_LK  = threading.Lock()
_GW_CACHE     = {}            # target-ip -> resolved gateway row (or None)
_GW_CACHE_LK  = threading.Lock()

def register_clusters(clusters):
    """Let the module-level SSH helpers resolve rows / gateways by hostname."""
    CLUSTER_INDEX.clear()
    for c in clusters:
        if c.get("hostname"): CLUSTER_INDEX[c["hostname"]] = c

def reset_gateways():
    """Forget cached routes + drop live gateway links.

    Call whenever the network might have changed (link up/down, Reload) so routes
    are re-decided against the new reachability.
    """
    with _GW_CACHE_LK: _GW_CACHE.clear()
    with _REACH_MEMO_LK: _REACH_MEMO.clear()
    with _GW_POOLS_LK:
        for link in _GW_POOLS.values():
            try: link.close()
            except: pass
        _GW_POOLS.clear()

def _prefix_score(a, b):
    """How many leading octets two dotted-quad IPs share (0..4)."""
    n = 0
    for x, y in zip(a.split("."), b.split(".")):
        if x == y: n += 1
        else: break
    return n

def _direct_reachable(ip, port=22, timeout=2):
    if not ip: return False
    try:
        with socket.create_connection((ip, port), timeout=timeout): return True
    except: return False

_REACH_MEMO    = {}           # ip -> (reachable, expires_at)
_REACH_MEMO_LK = threading.Lock()
def _reachable_cached(ip, ttl=8):
    """_direct_reachable with a short TTL so resolving several hidden nodes in
    one refresh doesn't re-probe the same down peers over and over."""
    now = time.time()
    with _REACH_MEMO_LK:
        hit = _REACH_MEMO.get(ip)
        if hit and hit[1] > now: return hit[0]
    ok = _direct_reachable(ip)
    with _REACH_MEMO_LK:
        _REACH_MEMO[ip] = (ok, now + ttl)
    return ok

def _auto_gateway_for(r):
    """Best *reachable* jump among the other clusters. A gateway is only useful
    if we can actually reach it, so unreachable candidates are skipped even when
    their IP is numerically closer. Among reachable ones, closest IP wins."""
    tip = r.get("ip", "")
    if not tip: return None
    cands = []
    for c in CLUSTER_INDEX.values():
        if c.get("hostname") == r.get("hostname"): continue
        cip = c.get("ip", "")
        if cip: cands.append((_prefix_score(tip, cip), c))
    cands.sort(key=lambda t: t[0], reverse=True)   # closest first
    for _, c in cands:                              # first reachable wins
        if _reachable_cached(c["ip"]): return c
    return None

def _gateway_row_for(r, probe=True):
    """Resolve which node (if any) to tunnel through to reach row r. None = go direct."""
    via = (r.get("via") or "").strip()                    # 1) per-node override
    if via:
        return CLUSTER_INDEX.get(via) or next(
            (c for c in CLUSTER_INDEX.values() if c.get("ip") == via), None)
    gw_name = (CFG.get("gateway") or "").strip()           # 2) global gateway
    if gw_name:
        gw = CLUSTER_INDEX.get(gw_name)
        if gw and gw.get("hostname") != r.get("hostname"): return gw
    if not CFG.get("auto_gateway", True): return None      # 3) auto
    tip = r.get("ip", "")
    with _GW_CACHE_LK:
        if tip in _GW_CACHE: return _GW_CACHE[tip]
    gw = None
    if probe and not _direct_reachable(tip):
        gw = _auto_gateway_for(r)
    with _GW_CACHE_LK:
        _GW_CACHE[tip] = gw
    return gw


class _GatewayLink:
    """One persistent SSH connection to a gateway node, handing out cheap
    direct-tcpip channels to the internal targets it can see. Reusing a single
    connection keeps us well under sshd MaxStartups even with parallel transfers."""
    def __init__(self, row):
        self.row  = row
        self._ssh = None
        self._lk  = threading.Lock()

    def _ensure(self):
        t = self._ssh.get_transport() if self._ssh else None
        if t and t.is_active(): return
        ssh = SSHClient(); ssh.set_missing_host_key_policy(AutoAddPolicy())
        ssh.connect(hostname=self.row["ip"], username=self.row["username"],
                    password=self.row.get("password") or None,
                    key_filename=self.row.get("key") or None,
                    timeout=CFG["ssh_timeout"]+7,
                    banner_timeout=CFG["banner_timeout"], auth_timeout=20)
        try: ssh.get_transport().set_keepalive(15)
        except: pass
        self._ssh = ssh

    def channel(self, ip, port=22, timeout=None):
        """A fresh TCP tunnel gateway -> (ip:port). Caller owns and closes it."""
        with self._lk:
            self._ensure()
            t = self._ssh.get_transport()
        return t.open_channel("direct-tcpip", (ip, port), ("127.0.0.1", 0),
                              timeout=timeout or (CFG["ssh_timeout"]+5))

    def close(self):
        try:
            if self._ssh: self._ssh.close()
        except: pass
        self._ssh = None

def _gateway_link(gw_row):
    with _GW_POOLS_LK:
        link = _GW_POOLS.get(gw_row["hostname"])
        if link is None:
            link = _GatewayLink(gw_row); _GW_POOLS[gw_row["hostname"]] = link
        return link

def _tunnel_channel(gw_row, ip, port=22, timeout=None):
    return _gateway_link(gw_row).channel(ip, port, timeout)


# ═══════════════════════════════════════════════════════════════════════════════
#   SSH / PROBE
# ═══════════════════════════════════════════════════════════════════════════════
REMOTE_SCRIPT = r"""
CPU=$(top -bn1 2>/dev/null | grep -i "Cpu(s)" | awk '{print 100 - $8}')
[ -z "$CPU" ] && CPU=$(top -l1 2>/dev/null | awk '/CPU usage/ {gsub("%","",$3); print $3}')
RAM=$(free -m 2>/dev/null | awk '/Mem:/ {printf("%.1f"), $3/$2*100}')
RUSED=$(free -m 2>/dev/null | awk '/Mem:/ {printf("%.0f", $3)}')   # RAM used  (MB)
RTOT=$(free -m 2>/dev/null | awk '/Mem:/ {printf("%.0f", $2)}')    # RAM total (MB)
if [ -z "$RAM" ]; then                                             # macOS fallback
  RAM=$(vm_stat 2>/dev/null | awk '/Pages active/{a=$3} /Pages wired/{w=$4} /Pages free/{f=$3} END{u=(a+w); printf("%.1f", u/(u+f)*100)}')
  RTOT=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf("%.0f", $1/1048576)}')
  RUSED=$(awk -v p="$RAM" -v t="$RTOT" 'BEGIN{printf("%.0f", p/100*t)}')
fi
NPROC=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null)
UP=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
[ -z "$UP" ] && UP=$(sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,]' '{print systime()-$4}')
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -1)
  GMEM=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | head -1 | sed 's/, /\//')
else
  GPU="N/A"; GMEM="N/A"
fi
# Storage on the root filesystem. POSIX df (-P) prints one line per fs (no
# wrapping on long device names) and -k forces 1024-byte blocks, so the same
# awk works on Linux and BSD/macOS. DISK=used%, DFREE/DTOT in whole GB.
DISK=$(df -Pk / 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5+0}')
DFREE=$(df -Pk / 2>/dev/null | awk 'NR==2{printf("%.0f", $4/1048576)}')
DTOT=$(df -Pk / 2>/dev/null | awk 'NR==2{printf("%.0f", $2/1048576)}')
SIM_SLURM=0
command -v squeue >/dev/null 2>&1 && SIM_SLURM=$(squeue -h -u $USER 2>/dev/null | wc -l | tr -d ' ')
# Non-Slurm long-running jobs, matched against the user's own regex
# (CFG["sim_process_pattern"]). Blank pattern = skip this count entirely.
SIM_PROC=0
[ -n "__SIM_PATTERN__" ] && SIM_PROC=$(ps -eo comm,args 2>/dev/null | grep -iE '__SIM_PATTERN__' | grep -v grep | wc -l | tr -d ' ')
SIM_TOTAL=$((SIM_SLURM + SIM_PROC))
HAS_SLURM=0
command -v sinfo >/dev/null 2>&1 && HAS_SLURM=1
[ "$HAS_SLURM" = "0" ] && command -v squeue >/dev/null 2>&1 && HAS_SLURM=1
echo "${CPU:-0}|${RAM:-0}|${GPU}|${GMEM}|${NPROC:-?}|${UP:-0}|${SIM_TOTAL:-0}|${SIM_SLURM:-0}|${SIM_PROC:-0}|${HAS_SLURM:-0}|${DISK:-0}|${DFREE:-0}|${DTOT:-0}|${RUSED:-0}|${RTOT:-0}"
"""

def is_online(target, port=22, timeout=3):
    """True if target:22 accepts a connection — directly, or tunneled through a
    gateway when the network doesn't route to it. `target` may be a cluster row
    (preferred — enables the gateway fallback) or a bare IP string."""
    row = target if isinstance(target, dict) else {"ip": target}
    ip  = row.get("ip", "")
    if not ip: return False
    if _direct_reachable(ip, port, timeout):                 # 1) direct
        return True
    gw = _gateway_row_for(row, probe=False)                  # 2) via a gateway
    if not gw and CFG.get("auto_gateway", True):
        gw = _auto_gateway_for(row)
        with _GW_CACHE_LK: _GW_CACHE[ip] = gw
    if not gw: return False
    try:
        ch = _tunnel_channel(gw, ip, port, timeout=timeout)
        try:
            ch.settimeout(timeout)
            return ch.recv(4)[:3] == b"SSH"                  # sshd greets at once
        finally:
            try: ch.close()
            except: pass
    except Exception:
        return False

def _ssh_cmd_parts(r, x11=False):
    base = ["ssh"]
    if x11:                       # -X = X11 forwarding, so remote GUI apps display
        base.append("-X")         #      locally. Only for interactive terminals,
                                  #      never for the metric probes.
    base += ["-o","StrictHostKeyChecking=no","-o",f"ConnectTimeout={CFG['ssh_timeout']}"]
    gw = _gateway_row_for(r)
    if gw:
        # Relay this connection through the gateway. `-W %h:%p` makes ssh forward
        # stdio straight to the target via the jump — files land on the target,
        # never on the jump. (Same effect as `ssh -J`.)
        jbase = (f"ssh -W %h:%p -o StrictHostKeyChecking=no "
                 f"-o ConnectTimeout={CFG['ssh_timeout']}")
        if gw.get("key"):
            jump = f"{jbase} -i {shlex.quote(os.path.expanduser(gw['key']))} {gw['username']}@{gw['ip']}"
        elif gw.get("password") and shutil.which("sshpass"):
            jump = f"sshpass -p {shlex.quote(gw['password'])} {jbase} {gw['username']}@{gw['ip']}"
        else:
            jump = f"{jbase} {gw['username']}@{gw['ip']}"
        base += ["-o", f"ProxyCommand={jump}"]
    if r.get("key"):
        return base + ["-i", os.path.expanduser(r["key"]), f"{r['username']}@{r['ip']}"]
    if r.get("password") and shutil.which("sshpass"):
        return ["sshpass","-p",r["password"]] + base + [f"{r['username']}@{r['ip']}"]
    return base + ["-o","BatchMode=yes", f"{r['username']}@{r['ip']}"]

def _remote_script():
    """REMOTE_SCRIPT with the user's simulation-process regex substituted in.

    CFG["sim_process_pattern"] is an extended-regex matching the long-running
    job processes YOU care about, e.g. "my-solver|run\\.sh". Blank (default)
    disables the process count — Slurm jobs are still counted."""
    pat = str(CFG.get("sim_process_pattern", "") or "")
    # The pattern lands inside a single-quoted shell literal, where every
    # character except ' is taken literally — so backslashes reach grep intact.
    # Stripping ' and newlines is what stops a pattern from breaking out of the
    # quoting and injecting shell.
    pat = pat.replace("'", "").replace("\n", " ").replace("\r", " ")
    return REMOTE_SCRIPT.replace("__SIM_PATTERN__", pat)

def get_remote_status(r):
    try:
        argv = _ssh_cmd_parts(r) + [_remote_script()]
        out = subprocess.check_output(argv, stderr=subprocess.DEVNULL,
                                      timeout=CFG["ssh_timeout"]+5).decode().strip()
        lines = [l for l in out.splitlines() if "|" in l]
        if not lines: return None
        p = lines[-1].split("|")
        while len(p) < 15: p.append("0")   # older nodes omit the disk / ram-size fields
        return {"cpu":p[0]or"0","ram":p[1]or"0","gpu":p[2]or"N/A",
                "gpu_mem":p[3]or"N/A","nproc":p[4]or"?","uptime":p[5]or"0",
                "sim_total":p[6]or"0","sim_slurm":p[7]or"0","sim_proc":p[8]or"0",
                "has_slurm":p[9]or"0","disk":p[10]or"0",
                "disk_free":p[11]or"0","disk_total":p[12]or"0",
                "ram_used":p[13]or"0","ram_total":p[14]or"0"}
    except: return None

def get_slurm_status(r, cfg):
    user  = cfg.get("user") or r["username"]
    part  = cfg.get("partition","")
    limit = cfg.get("core_limit", cfg.get("node_limit", 0))   # CORES per user (0 = auto)
    pflag = f"-p {part}" if part else ""
    remote = f"""
echo '@@JOBS@@'
squeue -h -u {user} {pflag} -o '%i|%j|%T|%D|%C|%M|%R' 2>/dev/null
echo '@@NODES_ALLOC@@'
squeue -h -u {user} -t RUNNING {pflag} -o '%D' 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'
echo '@@CORES@@'
squeue -h -u {user} -t RUNNING {pflag} -o '%C' 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'
echo '@@CORELIMIT@@'
sacctmgr -n -P show assoc where user="{user}" format=MaxTRES 2>/dev/null | tr ',' '\\n' | sed -n 's/^cpu=//p' | head -1
echo '@@SINFO@@'
sinfo -h {pflag} -o '%P|%a|%D|%t' 2>/dev/null
echo '@@NODELIST@@'
sinfo -N -h {pflag} -o '%N|%T|%C|%c|%m|%G' 2>/dev/null
echo '@@END@@'
"""
    try:
        argv = _ssh_cmd_parts(r) + [remote]
        out = subprocess.check_output(argv, stderr=subprocess.DEVNULL,
                                      timeout=CFG["ssh_timeout"]+10).decode()
    except: return None

    def section(name):
        try:
            seg = out.split(f"@@{name}@@",1)[1].split("@@",1)[0]
            return [l for l in seg.strip().splitlines() if l.strip()]
        except: return []

    jobs = []
    for line in section("JOBS"):
        f = line.split("|")
        if len(f) >= 7:
            jobs.append({"id":f[0],"name":f[1],"state":f[2],
                         "nodes":f[3],"cores":f[4],"time":f[5],"reason":f[6]})

    def first_int(sec):
        s = section(sec)
        return int(s[0]) if s and s[0].strip().isdigit() else 0

    part_states = {}
    for line in section("SINFO"):
        f = line.split("|")
        if len(f) >= 4:
            pname,_,cnt,state = f[0],f[1],f[2],f[3]
            try: cnt = int(cnt)
            except: cnt = 0
            part_states.setdefault(pname,{}).setdefault(state,0)
            part_states[pname][state] += cnt

    seen = {}
    for line in section("NODELIST"):
        f = line.split("|")
        if len(f) >= 6:
            name,state,cores_str,cpus,mem,gres = f[0],f[1],f[2],f[3],f[4],f[5]
            try:
                pp = cores_str.split("/")
                alloc=int(pp[0]); idle=int(pp[1]); total=int(pp[3]) if len(pp)>3 else alloc+idle
            except: alloc=idle=total=0
            if name not in seen:
                seen[name]={"name":name,"state":state,"cores_alloc":alloc,
                            "cores_idle":idle,"cores_total":total,"gres":gres or "none"}

    nodes = list(seen.values())
    cl_total = sum(n["cores_total"] for n in nodes)
    cl_alloc = sum(n["cores_alloc"] for n in nodes)
    cl_idle  = sum(n["cores_idle"]  for n in nodes)
    detected_limit = first_int("CORELIMIT")          # per-user CPU cap from sacctmgr
    core_limit = limit or detected_limit             # explicit cfg wins, else detected

    return {"user":user,
            "core_limit":core_limit, "detected_limit":detected_limit,
            "cores_used":first_int("CORES"), "nodes_used":first_int("NODES_ALLOC"),
            "cluster_cores_total":cl_total, "cluster_cores_alloc":cl_alloc,
            "cluster_cores_idle":cl_idle, "nodes_total":len(nodes),
            "jobs":jobs, "part_states":part_states, "nodelist":nodes,
            "running":sum(1 for j in jobs if j["state"]=="RUNNING"),
            "pending":sum(1 for j in jobs if j["state"]=="PENDING")}


# ═══════════════════════════════════════════════════════════════════════════════
#   CONNECTION HANDLING  — retry + backoff (the throttle-safe core)
# ═══════════════════════════════════════════════════════════════════════════════
def connect_ssh(r, retries=None, banner_timeout=None):
    """Open an SSHClient to r with bounded retries and exponential backoff.

    Retrying with backoff is what survives sshd MaxStartups / per-source
    throttling — the server resets the banner under load; a short pause lets
    the pending-connection queue drain instead of hammering it.

    If r isn't directly routable (typical when only a couple of nodes are
    exposed externally), the connection is transparently tunneled through a
    gateway node using an SSH direct-tcpip channel. The target session — and any
    SFTP opened on it — then streams end-to-end and writes straight to the
    target. Nothing is staged on the gateway, so there is no double copy.
    """
    retries = CFG["connect_retries"] if retries is None else retries
    bt      = CFG["banner_timeout"]  if banner_timeout is None else banner_timeout
    gw      = _gateway_row_for(r)        # may probe once, then caches the route
    last = None
    for attempt in range(max(1, retries)):
        sock = None
        try:
            if gw:
                sock = _tunnel_channel(gw, r["ip"], 22)
            ssh = SSHClient(); ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(hostname=r["ip"], username=r["username"],
                        password=r.get("password") or None,
                        key_filename=r.get("key") or None,
                        timeout=CFG["ssh_timeout"]+7,
                        banner_timeout=bt, auth_timeout=20, sock=sock)
            return ssh
        except Exception as e:
            last = e
            if sock is not None:
                try: sock.close()
                except: pass
            # If the gateway link itself died, drop it so the next attempt rebuilds it.
            if gw and isinstance(e, (ChannelException, EOFError, OSError)):
                try: _gateway_link(gw).close()
                except: pass
            if attempt < retries - 1:
                time.sleep(min(BACKOFF_BASE * (2 ** attempt), 12))
    raise last

def sftp_connect(r, **kw):
    ssh = connect_ssh(r, **kw)
    return ssh, ssh.open_sftp()


def open_ssh_terminal(r):
    parts = _ssh_cmd_parts(r, x11=CFG.get("x11_forward", True))
    host  = r.get("hostname","node")
    # On POSIX the command is handed to a shell, so each arg must be quoted —
    # otherwise a multi-word ProxyCommand=... value gets split apart.
    cmd_posix = " ".join(shlex.quote(p) for p in parts)
    cmd_win   = " ".join(parts)
    if OSNAME == "darwin":
        script = (f'tell application "Terminal"\n activate\n'
                  f' do script "echo -n -e \\"\\\\033]0;{host}\\\\007\\"; {cmd_posix}"\n'
                  f'end tell')
        subprocess.Popen(["osascript","-e",script])
    elif OSNAME == "linux":
        for t in ("gnome-terminal","konsole","xfce4-terminal","xterm"):
            if shutil.which(t):
                if t == "gnome-terminal":
                    subprocess.Popen([t,"--title",host,"--","bash","-c",f"{cmd_posix}; exec bash"])
                else:
                    subprocess.Popen([t,"-e",f"bash -c '{cmd_posix}; exec bash'"])
                return
    elif OSNAME == "windows":
        subprocess.Popen(f'start "{host}" cmd /k {cmd_win}', shell=True)


# ═══════════════════════════════════════════════════════════════════════════════
#   REMOTE DESKTOP   (one-click screen sharing)
#   ---------------------------------------------------------------------------
#   Pressing "Desktop" on a card:
#     1. SSHes into the node (via connect_ssh → gateways/retries reused)
#     2. in the node's host.py folder (host_dir column, else ~/.pydesk), detects
#        the live X11 display + Xauthority cookie and launches host.py detached
#     3. waits for the stream port to open, then runs viewer.py locally
#
#   host.py / protocol.py must exist in the remote host_dir. viewer.py (+ its
#   protocol.py) must sit next to this file on THIS machine. If host.py/protocol
#   are found locally too, they're SFTP-refreshed to the remote so versions match.
# ═══════════════════════════════════════════════════════════════════════════════
PYDESK_PORT = 5900
_APP_DIR    = Path(__file__).resolve().parent


def _rd_remote_command(password, host_dir):
    """Bash for the remote: pick working dir, find X display+cookie, launch host."""
    pw  = shlex.quote(password)
    wd  = shlex.quote(host_dir) if host_dir else "~/.pydesk"
    return (
        f"mkdir -p {wd} 2>/dev/null ; cd {wd} 2>/dev/null || cd ~/.pydesk ; "
        # locate the active X11 display (X1 -> :1) and the session's auth cookie
        'export DISPLAY="${DISPLAY:-$(ls /tmp/.X11-unix/ 2>/dev/null '
        "| sed 's/X/:/' | head -1)}\" ; "
        'export DISPLAY="${DISPLAY:-:0}" ; '
        'export XAUTHORITY="$(find /run/user/$(id -u) /var/run/lightdm "$HOME" '
        '-maxdepth 3 \\( -name Xauthority -o -name .Xauthority \\) '
        '2>/dev/null | head -1)" ; '
        # install capture deps only if missing (fast after the first time)
        'python3 -c "import mss,cv2,numpy,pynput" 2>/dev/null || '
        "python3 -m pip install --user --quiet mss opencv-python numpy pynput ; "
        f'pkill -f "host.py --password .* --port {PYDESK_PORT}" 2>/dev/null ; '
        f"nohup python3 host.py --password {pw} --port {PYDESK_PORT} "
        "> ~/.pydesk/pydesk.log 2>&1 & echo STARTED"
    )


def _rd_port_open(ip, port, timeout=1.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _rd_sftp_refresh(ssh, host_dir):
    """Best-effort: copy local host.py/protocol.py to the remote working dir."""
    local = {f: _APP_DIR / f for f in ("host.py", "protocol.py")}
    if not all(p.exists() for p in local.values()):
        return                                  # nothing local to push; assume present
    try:
        sftp = ssh.open_sftp()
        wd = host_dir or f"{sftp.normalize('.')}/.pydesk"
        try: sftp.mkdir(wd)
        except IOError: pass
        for fname, p in local.items():
            sftp.put(str(p), f"{wd}/{fname}")
        sftp.close()
    except Exception:
        pass


def open_remote_desktop(r, on_status=None):
    """Threaded: start host.py on the node, then open the local viewer."""
    def say(msg, ok=True):
        if on_status:
            try: on_status(r, msg, ok)
            except Exception: pass

    def worker():
        ip   = r.get("ip", "")
        pw   = r.get("password", "")
        host = r.get("hostname", "node")
        hdir = r.get("host_dir", "")

        viewer = _APP_DIR / "viewer.py"
        if not viewer.exists():
            say("viewer.py not found next to clusteros.py", ok=False); return

        try:
            say(f"SSH → {host} …")
            ssh = connect_ssh(r)
        except Exception as e:
            say(f"SSH failed: {e}", ok=False); return

        try:
            _rd_sftp_refresh(ssh, hdir)
            say("Starting remote desktop host …")
            _in, out, _err = ssh.exec_command(_rd_remote_command(pw, hdir))
            out.read()                          # wait for the launch to return

            say("Waiting for host to come online …")
            deadline = time.time() + 45
            while time.time() < deadline:
                if _rd_port_open(ip, PYDESK_PORT):
                    break
                time.sleep(1.5)
            else:
                reason = ""
                try:
                    _, log, _ = ssh.exec_command("tail -n 6 ~/.pydesk/pydesk.log")
                    lines = [l for l in log.read().decode("utf-8","ignore").splitlines() if l.strip()]
                    reason = lines[-1][:160] if lines else ""
                except Exception:
                    pass
                say("Host didn't start" + (f" → {reason}" if reason else ""), ok=False)
                return
        finally:
            try: ssh.close()
            except Exception: pass

        say(f"Opening desktop · {host}")
        subprocess.Popen([sys.executable, str(viewer),
                          "--host", ip, "--port", str(PYDESK_PORT),
                          "--password", pw])

    threading.Thread(target=worker, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
#   HOST CARD  — responsive canvas tile
# ═══════════════════════════════════════════════════════════════════════════════
class HostCard:
    SLURM_H = 276          # tile height when Slurm-capable (+26 for DISK gauge)
    PLAIN_H = 234          # tile height otherwise          (+26 for DISK gauge)

    def __init__(self, parent, row, on_click, on_detail=None, on_transfer=None,
                 on_resize=None):
        self.row        = row
        self.on_click   = on_click
        self.on_detail  = on_detail
        self.on_transfer= on_transfer
        self.on_resize  = on_resize
        self.host       = row["hostname"]
        # Slurm capability: pinned via SLURM_HOSTS, or auto-detected at probe time.
        self.slurm_pinned = row["hostname"] in SLURM_HOSTS
        self.has_slurm    = self.slurm_pinned
        self.is_slurm     = self.has_slurm
        self.H          = self.SLURM_H if self.is_slurm else self.PLAIN_H
        self.spark      = deque(maxlen=SPARK_LEN)
        self.target     = {"cpu":0,"ram":0,"gpu":0,"disk":0}
        self.shown      = {"cpu":0,"ram":0,"gpu":0,"disk":0}
        self.online     = False
        self.status     = "checking…"
        self.meta       = {}
        self.slurm      = None
        self._ssh_box = self._detail_box = self._transfer_box = None
        self._rd_box  = None
        self._rd_msg  = ""            # transient Remote-Desktop status text
        self._rd_ok   = True
        self._rd_until = 0
        self._hovered = False

        self.cv = tk.Canvas(parent, height=self.H, bg=T["bg"],
                            highlightthickness=0, bd=0)
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Enter>",    lambda e: self._hover(True))
        self.cv.bind("<Leave>",    lambda e: self._hover(False))
        self.cv.bind("<Configure>",lambda e: self.draw())

    def set_slurm_capable(self, flag):
        """Flip the card between Slurm and plain layouts when detection changes.
        Grows/shrinks the tile and asks the grid to reflow if the height moved."""
        flag = bool(flag) or self.slurm_pinned
        if flag == self.is_slurm:
            return
        self.is_slurm = self.has_slurm = flag
        newH = self.SLURM_H if flag else self.PLAIN_H
        if newH != self.H:
            self.H = newH
            try: self.cv.configure(height=self.H)
            except Exception: pass
            if self.on_resize:
                try: self.on_resize()
                except Exception: pass
        self.draw()

    # ── interaction ──
    def _click(self, e):
        for box, cb in ((self._detail_box, self.on_detail),
                        (self._transfer_box, self.on_transfer),
                        (self._rd_box, lambda r: self._start_remote_desktop()),
                        (self._ssh_box, lambda r: open_ssh_terminal(r))):
            if box and cb:
                x1,y1,x2,y2 = box
                if x1<=e.x<=x2 and y1<=e.y<=y2:
                    cb(self.row); return
        self.on_click(self.row)

    # ── remote desktop ──
    def _start_remote_desktop(self):
        open_remote_desktop(self.row, on_status=self._rd_status)

    def _rd_status(self, row, msg, ok=True):
        """Thread-safe status callback from open_remote_desktop."""
        def apply():
            self._rd_msg, self._rd_ok = msg, ok
            self._rd_until = time.time() + (12 if not ok else 6)
            self.draw()
        try:
            self.cv.after(0, apply)
        except Exception:
            pass

    def _hover(self, on):
        self._hovered = on
        self.cv.config(cursor="hand2" if on else "")
        self.draw()

    def set_slurm(self, d): self.slurm = d; self.draw()
    def set_data(self, online, status, st):
        self.online, self.status = online, status
        if st:
            self.meta = st
            self.target = {"cpu":to_float(st["cpu"]) or 0,
                           "ram":to_float(st["ram"]) or 0,
                           "gpu":(to_float(st["gpu"]) or 0) if st["gpu"]!="N/A" else 0,
                           "disk":to_float(st.get("disk")) or 0}
            self.spark.append(self.target["cpu"])
        else:
            self.target = {"cpu":0,"ram":0,"gpu":0,"disk":0}

    def ease(self):
        moved = False
        for k in self.shown:
            d = self.target[k]-self.shown[k]
            if abs(d) > 0.4: self.shown[k] += d*0.25; moved = True
            else:            self.shown[k] = self.target[k]
        return moved

    # ── drawing primitives ──
    @staticmethod
    def _rr(cv,x1,y1,x2,y2,r,**kw):
        pts=[x1+r,y1,x2-r,y1,x2,y1,x2,y1+r,x2,y2-r,x2,y2,
             x2-r,y2,x1+r,y2,x1,y2,x1,y2-r,x1,y1+r,x1,y1]
        return cv.create_polygon(pts,smooth=True,**kw)

    def _bar(self,x,y,w,label,pct,color,val_text=None):
        cv=self.cv
        cv.create_text(x,y-12,text=label,anchor="w",fill=T["muted"],font=(MONO,9,"bold"))
        val="—" if not self.online else (val_text if val_text is not None else f"{pct:4.0f}%")
        cv.create_text(x+w,y-12,text=val,anchor="e",fill=color,font=(MONO,10,"bold"))
        self._rr(cv,x,y,x+w,y+7,4,fill=T["track"],outline="")
        fw=max(0,min(1,pct/100.0))*w
        if fw>3 and self.online:
            self._rr(cv,x,y,x+fw,y+7,4,fill=color,outline="")

    def _sparkline(self,x,y,w,h):
        if len(self.spark)<2 or not self.online: return
        cv=self.cv; mx=max(self.spark) or 1; n=len(self.spark)
        step=w/(SPARK_LEN-1); x0=x+(SPARK_LEN-n)*step
        pts=[]
        for i,v in enumerate(self.spark):
            pts+=[x0+i*step, y+h-(v/mx)*h]
        if len(pts)>=4:
            cv.create_line(*pts,fill=T["cyan"],width=1.6,smooth=True)

    def _btn(self,x,y,w,text,color):
        self._rr(self.cv,x,y,x+w,y+22,6,fill=T["raised"],outline=T["border2"])
        self.cv.create_text(x+w/2,y+11,text=text,fill=color,font=(MONO,9,"bold"),anchor="center")
        return (x,y,x+w,y+22)

    def draw(self):
        cv=self.cv; cv.delete("all")
        cv.config(bg=T["bg"])
        W=cv.winfo_width() or 360; H=self.H; pad=6

        bg = T["card2"] if self._hovered else T["card"]
        self._rr(cv,pad,pad,W-pad,H-pad,16,fill=bg,outline=T["border2"] if self._hovered else T["border"])

        if   self.status.startswith("ONLINE"): ac = lvl_color(max(self.shown.values()))
        elif "OFFLINE" in self.status:         ac = T["red"]
        elif "ERROR"   in self.status:         ac = T["amber"]
        else:                                  ac = T["dim"]

        self._rr(cv,pad,pad,pad+5,H-pad,4,fill=ac,outline="")

        cx,cy=28,30
        if self.status.startswith("ONLINE"):
            cv.create_oval(cx-11,cy-11,cx+11,cy+11,outline=ac,width=1,dash=(3,3))
        cv.create_oval(cx-6,cy-6,cx+6,cy+6,fill=ac,outline="")

        cv.create_text(46,23,text=self.host,anchor="w",fill=T["text"],font=(SANS,15,"bold"))
        cv.create_text(46,42,text=self.row["ip"],anchor="w",fill=T["muted"],font=(MONO,10))
        cv.create_text(46,60,text="● "+self.status,anchor="w",fill=ac,font=(MONO,9,"bold"))

        # transient Remote-Desktop status (set by the Desktop button)
        if self._rd_msg and time.time() < self._rd_until:
            rc  = T["cyan"] if self._rd_ok else T["red"]
            txt = self._rd_msg if len(self._rd_msg) <= 44 else self._rd_msg[:43] + "…"
            cv.create_text(46, 76, text="RD · " + txt, anchor="w",
                           fill=rc, font=(MONO, 8, "bold"))

        # sim badge / sparkline (top-right)
        spw=104
        self._sparkline(W-spw-22,18,spw,20)
        cv.create_text(W-22,46,text="cpu trend",anchor="e",fill=T["dim"],font=(MONO,8))
        if self.online and self.meta:
            sim=int(self.meta.get("sim_total","0") or 0)
            if sim>0:
                badge=f"▶ {sim} SIM"; tw=len(badge)*7+16
                self._rr(cv,W-22-tw,58,W-22,78,7,fill=T["card2"],outline=T["green"])
                cv.create_text(W-22-tw/2,68,fill=T["green"],font=(MONO,9,"bold"),text=badge,anchor="center")

        # gauges
        bx, bw = 22, W-44
        cpu_val=None
        if self.online and self.meta:
            ncores=to_float(self.meta.get("nproc","0")) or 0
            if ncores:
                ucores=self.shown["cpu"]/100.0*ncores
                cpu_val=f"{self.shown['cpu']:.0f}% · {ucores:.0f}/{ncores:.0f} cores"
        self._bar(bx, 96, bw,"CPU",self.shown["cpu"],
                  lvl_color(self.shown["cpu"]) if self.online else T["dim"], val_text=cpu_val)
        ram_val=None
        if self.online and self.meta:
            rtot=to_float(self.meta.get("ram_total","0")) or 0          # MB
            if rtot:
                rused=to_float(self.meta.get("ram_used","0")) or 0      # MB
                ram_val=f"{self.shown['ram']:.0f}% · {rused/1024:.1f}/{rtot/1024:.1f}G"
        self._bar(bx,122, bw,"RAM",self.shown["ram"],
                  lvl_color(self.shown["ram"]) if self.online else T["dim"], val_text=ram_val)
        if self.online and self.meta.get("gpu")=="N/A":
            cv.create_text(bx,148-12,text="GPU",anchor="w",fill=T["muted"],font=(MONO,9,"bold"))
            cv.create_text(bx+bw,148-12,text="N/A",anchor="e",fill=T["dim"],font=(MONO,10,"bold"))
            self._rr(cv,bx,148,bx+bw,148+7,4,fill=T["track"],outline="")
        else:
            gpu_val=None
            gm=self.meta.get("gpu_mem","N/A") if self.meta else "N/A"
            if self.online and gm and gm!="N/A" and "/" in gm:
                gu=to_float(gm.split("/")[0]); gt=to_float(gm.split("/")[1])
                if gu is not None and gt:
                    gpu_val=f"{self.shown['gpu']:.0f}% · {gu/1024:.1f}/{gt/1024:.1f}G"
            self._bar(bx,148,bw,"GPU",self.shown["gpu"],
                      lvl_color(self.shown["gpu"]) if self.online else T["dim"], val_text=gpu_val)

        # storage gauge — bar fills with used% (red = nearly full); label = free space
        dtot = to_float(self.meta.get("disk_total","0")) if self.meta else 0
        if self.online and dtot:
            dfree = to_float(self.meta.get("disk_free","0")) or 0
            dused = max(0, dtot - dfree)
            self._bar(bx,174,bw,"DISK",self.shown["disk"],
                      lvl_color(self.shown["disk"]),
                      val_text=f"{self.shown['disk']:.0f}% · {dused:.0f}/{dtot:.0f}G")
        elif self.online:
            cv.create_text(bx,174-12,text="DISK",anchor="w",fill=T["muted"],font=(MONO,9,"bold"))
            cv.create_text(bx+bw,174-12,text="N/A",anchor="e",fill=T["dim"],font=(MONO,10,"bold"))
            self._rr(cv,bx,174,bx+bw,174+7,4,fill=T["track"],outline="")
        else:
            self._bar(bx,174,bw,"DISK",0,T["dim"])

        # meta line
        if self.online and self.meta:
            cv.create_text(bx,198,anchor="w",fill=T["dim"],font=(MONO,9),
                text=f"cores {self.meta.get('nproc','?')}   "
                     f"up {fmt_uptime(self.meta.get('uptime',0))}   "
                     f"vram {self.meta.get('gpu_mem','—')}")

        # slurm mini row  — shows the per-user CORE allocation (not nodes)
        self._detail_box = None
        if self.is_slurm:
            sy=216; s=self.slurm
            if s:
                used  = s.get("cores_used",0)
                limit = s.get("core_limit",0)
                cv.create_text(bx,sy,anchor="w",fill=T["muted"],font=(MONO,9,"bold"),text="CORES")
                lx=bx+58; lw=W-lx-130
                self._rr(cv,lx,sy-5,lx+lw,sy+4,4,fill=T["track"],outline="")
                if limit:                                  # bar = used / per-user cap
                    pct=min(100,used/limit*100)
                    nc=T["red"] if pct>=90 else (T["amber"] if pct>=70 else T["violet"])
                    fw=min(used/limit,1.0)*lw
                    if fw>3: self._rr(cv,lx,sy-5,lx+fw,sy+4,4,fill=nc,outline="")
                    cv.create_text(lx+lw+8,sy,anchor="w",fill=nc,font=(MONO,9,"bold"),
                                   text=f"{used}/{limit}")
                else:                                      # no cap → show cluster busy
                    tot=s.get("cluster_cores_total",0) or 1
                    busy=s.get("cluster_cores_alloc",0)
                    fw=min(busy/tot,1.0)*lw
                    if fw>3: self._rr(cv,lx,sy-5,lx+fw,sy+4,4,fill=T["violet"],outline="")
                    cv.create_text(lx+lw+8,sy,anchor="w",fill=T["violet"],font=(MONO,9,"bold"),
                                   text=f"{used} used")
                cv.create_text(bx,sy+16,anchor="w",fill=T["dim"],font=(MONO,8),
                               text=f"run {s['running']}  pend {s['pending']}  "
                                    f"nodes {s.get('nodes_used',0)}")
            else:
                cv.create_text(bx,sy,anchor="w",fill=T["dim"],font=(MONO,9),
                               text="SLURM  querying…" if self.online else "SLURM  —")

        # action buttons (bottom)
        by=H-32
        self._ssh_box      = self._btn(W-76,  by, 54,"⧉ SSH",     T["green"])
        self._transfer_box = self._btn(W-150, by, 68,"⇄ Transfer", T["cyan"])
        self._rd_box       = self._btn(W-232, by, 74,"▣ Desktop",  T["violet"])
        if self.is_slurm:
            self._detail_box = self._btn(22, by, 96,"⊞ Slurm detail", T["violet"])


# ═══════════════════════════════════════════════════════════════════════════════
#   REMOTE FILE BROWSER
# ═══════════════════════════════════════════════════════════════════════════════
class RemoteFileBrowser(ctk.CTkToplevel):
    def __init__(self, parent, cluster, on_select=None):
        super().__init__(parent)
        self.title(f"Browse · {cluster['hostname']}  ({cluster['ip']})")
        self.geometry("740x580"); self.minsize(560,420)
        self.configure(fg_color=T["bg"])
        self.grab_set(); self.lift(); self.focus_force()
        self._cluster=cluster; self._on_select=on_select
        self._ssh=self._sftp=None; self._cwd="/"; self._entries=[]
        self._sel=tk.StringVar()
        self._build()
        threading.Thread(target=self._connect,daemon=True).start()

    def _build(self):
        nav=ctk.CTkFrame(self,fg_color=T["panel"],corner_radius=0,height=50)
        nav.pack(fill="x"); nav.pack_propagate(False)
        ctk.CTkLabel(nav,text="◈ Remote Explorer",font=F(13,True),text_color=T["cyan"]
                     ).pack(side="left",padx=14,pady=12)
        self._dot=ctk.CTkLabel(nav,text="● Connecting…",font=FM(11),text_color=T["muted"])
        self._dot.pack(side="right",padx=14)

        pb=ctk.CTkFrame(self,fg_color=T["raised"],corner_radius=0,height=34)
        pb.pack(fill="x"); pb.pack_propagate(False)
        self._up=ctk.CTkButton(pb,text="↑",width=34,height=26,fg_color=T["border2"],
            hover_color=T["cyan"],text_color=T["text"],font=F(13,True),
            corner_radius=5,command=self._go_up,state="disabled")
        self._up.pack(side="left",padx=(8,6),pady=4)
        self._plbl=ctk.CTkLabel(pb,text=" Connecting…",font=FM(11),
                                text_color=T["muted"],anchor="w")
        self._plbl.pack(side="left",fill="x",expand=True)

        lf=ctk.CTkFrame(self,fg_color=T["card"],corner_radius=0); lf.pack(fill="both",expand=True)
        sb=tk.Scrollbar(lf,orient="vertical",width=8,bd=0,highlightthickness=0)
        sb.pack(side="right",fill="y")
        self._lb=tk.Listbox(lf,bg=T["card"],fg=T["text"],
            selectbackground=T["card2"],selectforeground=T["cyan"],
            font=(MONO,12),borderwidth=0,highlightthickness=0,
            activestyle="none",yscrollcommand=sb.set,relief="flat")
        self._lb.pack(side="left",fill="both",expand=True,padx=4,pady=4)
        sb.config(command=self._lb.yview)
        self._lb.bind("<Double-Button-1>",self._dbl)
        self._lb.bind("<<ListboxSelect>>",self._pick)

        foot=ctk.CTkFrame(self,fg_color=T["panel"],corner_radius=0,height=54)
        foot.pack(fill="x",side="bottom"); foot.pack_propagate(False)
        pf=ctk.CTkFrame(foot,fg_color=T["raised"],corner_radius=8,height=34)
        pf.pack(side="left",fill="x",expand=True,padx=(14,10),pady=10); pf.pack_propagate(False)
        ctk.CTkLabel(pf,textvariable=self._sel,font=FM(11),
                     text_color=T["green"],anchor="w").pack(fill="x",expand=True,padx=10,pady=7)
        ctk.CTkButton(foot,text="Cancel",width=84,height=34,fg_color=T["raised"],
            hover_color=T["border2"],text_color=T["muted"],font=F(12),
            corner_radius=8,command=self._cancel).pack(side="right",padx=(0,12),pady=10)
        self._ok=ctk.CTkButton(foot,text="Select  ✓",width=110,height=34,
            fg_color=T["cyan"],hover_color=T["cyanh"],text_color=T["void"],
            font=F(12,True),corner_radius=8,command=self._confirm,state="disabled")
        self._ok.pack(side="right",padx=(0,6),pady=10)

    def _connect(self):
        try:
            self._ssh,self._sftp=sftp_connect(self._cluster)
            _,out,_=self._ssh.exec_command("echo $HOME")
            home=out.read().decode().strip() or f"/home/{self._cluster['username']}"
            self.after(0,lambda:self._list_dir(home))
            self.after(0,lambda:self._up.configure(state="normal"))
            self.after(0,lambda:self._dot.configure(
                text=f"● {self._cluster['hostname']}",text_color=T["green"]))
        except Exception as e:
            self.after(0,lambda err=e:self._err(str(err)))

    def _list_dir(self,path):
        if not self._sftp: return
        self._lb.delete(0,"end"); self._lb.insert("end","  ⏳ loading…")
        self._plbl.configure(text=f"  {path}")
        self._cwd=path; self._sel.set(""); self._ok.configure(state="disabled")
        def fetch():
            try: entries=self._sftp.listdir_attr(path)
            except Exception as e:
                self.after(0,lambda:self._err(str(e))); return
            dirs =sorted([e for e in entries if     stat.S_ISDIR(e.st_mode)],key=lambda x:x.filename.lower())
            files=sorted([e for e in entries if not stat.S_ISDIR(e.st_mode)],key=lambda x:x.filename.lower())
            items=[(True,d.filename,0) for d in dirs]+[(False,f.filename,f.st_size or 0) for f in files]
            self.after(0,lambda its=items:self._populate(its))
        threading.Thread(target=fetch,daemon=True).start()

    def _populate(self,items):
        self._lb.delete(0,"end"); self._entries=[]
        for is_dir,name,size in items:
            sfx=("/" if is_dir else f"   {human_size(size)}")
            self._lb.insert("end",f"  {'📁' if is_dir else '📄'}  {name}{sfx}")
            self._entries.append((is_dir,name))

    def _go_up(self): self._list_dir(os.path.dirname(self._cwd.rstrip("/")) or "/")

    def _dbl(self,_):
        idx=self._lb.curselection()
        if not idx or idx[0]>=len(self._entries): return
        is_dir,name=self._entries[idx[0]]
        if is_dir: self._list_dir(self._cwd.rstrip("/")+"/"+name)

    def _pick(self,_):
        idx=self._lb.curselection()
        if not idx or idx[0]>=len(self._entries): return
        _,name=self._entries[idx[0]]
        self._sel.set(self._cwd.rstrip("/")+"/"+name)
        self._ok.configure(state="normal")

    def _confirm(self):
        p=self._sel.get()
        if p and self._on_select: self._on_select(p)
        self._cleanup()
        try: super().destroy()
        except: pass

    def _cancel(self):
        self._cleanup()
        try: super().destroy()
        except: pass

    def _cleanup(self):
        try:
            if self._sftp: self._sftp.close()
            if self._ssh:  self._ssh.close()
        except: pass

    def destroy(self):
        self._cleanup()
        try: super().destroy()
        except: pass

    def _err(self,msg):
        self._plbl.configure(text=f"  ✗  {msg}")
        self._lb.delete(0,"end"); self._lb.insert("end",f"  ✗  {msg}")
        self._dot.configure(text="● Error",text_color=T["red"])


# ═══════════════════════════════════════════════════════════════════════════════
#   ENDPOINT PANEL  (Transfer tab)
# ═══════════════════════════════════════════════════════════════════════════════
class EndpointPanel(ctk.CTkFrame):
    def __init__(self, parent, label, accent, clusters, on_change=None, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self._label=label; self._accent=accent; self._clusters=clusters
        self._on_change=on_change
        self.type_var=tk.StringVar(value="local")
        self.path_var=tk.StringVar()
        self.clus_var=tk.StringVar(
            value=f"{clusters[0]['hostname']}  ·  {clusters[0]['ip']}" if clusters else "")
        self._combo=self._ip_lbl=self._user_lbl=None
        self._build()

    def _clabels(self):
        return [f"{c['hostname']}  ·  {c['ip']}" for c in self._clusters]

    def _build(self):
        card=ctk.CTkFrame(self,fg_color=T["card"],corner_radius=14,height=272,
                          border_width=1,border_color=T["border"])
        card.pack(fill="x"); card.pack_propagate(False)
        hdr=ctk.CTkFrame(card,fg_color=T["card2"],corner_radius=0,height=46)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        ctk.CTkFrame(hdr,fg_color=self._accent,width=3,corner_radius=0).pack(side="left",fill="y")
        ctk.CTkLabel(hdr,text=f"  {self._label}  ",font=F(9,True),
                     text_color=T["void"],fg_color=self._accent,corner_radius=4
                     ).pack(side="left",padx=(10,8),pady=13)
        ctk.CTkLabel(hdr,text="Source" if self._label=="FROM" else "Destination",
                     font=F(13,True),text_color=T["text"]).pack(side="left")
        tog=ctk.CTkFrame(hdr,fg_color=T["raised"],corner_radius=8)
        tog.pack(side="right",padx=12,pady=9)
        self._bl=ctk.CTkButton(tog,text="💻 Local",width=88,height=24,corner_radius=6,
            font=F(10,True),fg_color=self._accent,hover_color=T["cyanh"],text_color=T["void"],
            command=lambda:self._switch("local")); self._bl.pack(side="left",padx=(3,2),pady=3)
        self._bc=ctk.CTkButton(tog,text="🖥 Cluster",width=88,height=24,corner_radius=6,
            font=F(10),fg_color="transparent",hover_color=T["border2"],text_color=T["muted"],
            command=lambda:self._switch("cluster")); self._bc.pack(side="left",padx=(2,3),pady=3)

        self._body=ctk.CTkFrame(card,fg_color="transparent")
        self._body.pack(fill="x",padx=16,pady=(10,14))
        self._lv=ctk.CTkFrame(self._body,fg_color="transparent")
        self._cv=ctk.CTkFrame(self._body,fg_color="transparent")
        self._build_local(self._lv); self._build_cluster(self._cv)
        self._lv.pack(fill="x")

    def _path_row(self,parent,color,placeholder,browse):
        row=ctk.CTkFrame(parent,fg_color=T["raised"],corner_radius=10,
                         border_width=1,border_color=T["border"]); row.pack(fill="x")
        ctk.CTkEntry(row,textvariable=self.path_var,font=FM(11),fg_color="transparent",
                     border_width=0,text_color=color,height=38,placeholder_text=placeholder
                     ).pack(side="left",fill="x",expand=True,padx=(12,0))
        ctk.CTkFrame(row,fg_color=T["border2"],width=1).pack(side="left",fill="y",pady=8)
        ctk.CTkButton(row,text="Browse",width=86,height=38,fg_color="transparent",
            hover_color=T["card2"],text_color=self._accent,font=F(11,True),corner_radius=0,
            command=browse).pack(side="left")

    def _build_local(self,p):
        ctk.CTkLabel(p,text="Local path",font=F(10),text_color=T["muted"]).pack(anchor="w",pady=(0,6))
        self._path_row(p,T["text"],"Browse or type a path…",self._browse_local)

    def _build_cluster(self,p):
        ctk.CTkLabel(p,text="Cluster",font=F(10),text_color=T["muted"]).pack(anchor="w",pady=(0,6))
        self._combo=ctk.CTkComboBox(p,values=self._clabels(),variable=self.clus_var,
            font=F(12),height=38,fg_color=T["raised"],border_color=T["border2"],border_width=1,
            button_color=T["raised"],button_hover_color=T["border2"],
            dropdown_fg_color=T["card2"],text_color=T["text"],
            command=lambda v:self._on_cluster_pick()); self._combo.pack(fill="x",pady=(0,10))
        info=ctk.CTkFrame(p,fg_color=T["raised"],corner_radius=8,
                          border_width=1,border_color=T["border"]); info.pack(fill="x",pady=(0,10))
        ctk.CTkLabel(info,text="IP",font=F(9,True),text_color=T["dim"]).pack(side="left",padx=(12,4),pady=8)
        self._ip_lbl=ctk.CTkLabel(info,text="—",font=FM(11),text_color=T["cyanl"]); self._ip_lbl.pack(side="left",pady=8)
        ctk.CTkLabel(info,text="  /  ",font=F(10),text_color=T["dim"]).pack(side="left")
        ctk.CTkLabel(info,text="USER",font=F(9,True),text_color=T["dim"]).pack(side="left",padx=(0,4))
        self._user_lbl=ctk.CTkLabel(info,text="—",font=FM(11),text_color=T["muted"]); self._user_lbl.pack(side="left",pady=8)
        ctk.CTkLabel(p,text="Remote path",font=F(10),text_color=T["muted"]).pack(anchor="w",pady=(0,6))
        self._path_row(p,T["green"],"Browse remote or type path…",self._browse_remote)
        self._refresh_info()

    def _switch(self,t):
        self.type_var.set(t); self.path_var.set("")
        if t=="local":
            self._bl.configure(fg_color=self._accent,text_color=T["void"],font=F(10,True))
            self._bc.configure(fg_color="transparent",text_color=T["muted"],font=F(10))
            self._cv.pack_forget(); self._lv.pack(fill="x")
        else:
            self._bc.configure(fg_color=self._accent,text_color=T["void"],font=F(10,True))
            self._bl.configure(fg_color="transparent",text_color=T["muted"],font=F(10))
            self._lv.pack_forget(); self._cv.pack(fill="x"); self._refresh_info()
        if self._on_change: self._on_change()

    def _browse_local(self):
        menu=tk.Menu(self,tearoff=0,bg=T["card2"],fg=T["text"],
                     activebackground=self._accent,activeforeground=T["void"],
                     bd=0,relief="flat",font=(SANS,11))
        menu.add_command(label="  📄   Select file…    ",command=self._pick_local_file)
        menu.add_command(label="  📁   Select folder…  ",command=self._pick_local_folder)
        try: menu.tk_popup(self.winfo_pointerx(),self.winfo_pointery())
        finally: menu.grab_release()

    def _pick_local_file(self):
        self._apply_local_path(filedialog.askopenfilename(title="Select file"))

    def _pick_local_folder(self):
        self._apply_local_path(filedialog.askdirectory(title="Select folder"))

    def _apply_local_path(self,p):
        if p:
            self.path_var.set(p)
            if self._on_change: self._on_change()

    def _browse_remote(self):
        c=self.get_cluster()
        if not c: messagebox.showwarning("No cluster","No clusters loaded."); return
        def on_sel(path):
            self.path_var.set(path)
            if self._on_change: self._on_change()
        RemoteFileBrowser(self.winfo_toplevel(), cluster=c, on_select=on_sel)

    def _on_cluster_pick(self):
        self.path_var.set(""); self._refresh_info()
        if self._on_change: self._on_change()

    def _refresh_info(self):
        c=self.get_cluster()
        if self._ip_lbl:   self._ip_lbl.configure(text=c["ip"] if c else "—")
        if self._user_lbl: self._user_lbl.configure(text=c["username"] if c else "—")

    def get_cluster(self):
        lbl=self.clus_var.get()
        for c in self._clusters:
            if c["hostname"] in lbl: return c
        return self._clusters[0] if self._clusters else None

    def get_type(self): return self.type_var.get()
    def get_path(self): return self.path_var.get().strip()

    def prefill_cluster(self, cluster_row):
        self.clus_var.set(f"{cluster_row['hostname']}  ·  {cluster_row['ip']}")
        self._switch("cluster"); self._refresh_info()

    def update_clusters(self, clusters):
        self._clusters=clusters
        if self._combo:
            self._combo.configure(values=self._clabels())
            if clusters: self.clus_var.set(self._clabels()[0])
        self.path_var.set(""); self._refresh_info()


# ═══════════════════════════════════════════════════════════════════════════════
#   MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════
class ClusterOS(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ClusterOS — Mission Control")
        self.geometry(pick_window_geometry(self))
        self.minsize(1120,720)
        self.configure(fg_color=T["bg"])

        self._clusters    = load_clusters_xlsx()
        register_clusters(self._clusters)
        self._pool        = ThreadPoolExecutor(max_workers=CFG["monitor_workers"])
        self._cards       = {}
        self._alert_st    = {}
        self._cancel_flag = threading.Event()
        self._xfer_start  = 0.0
        self._cur_view    = "monitor"
        self._grid_cols   = 0

        # transfer widgets
        self._from_panel=self._to_panel=None
        self._progress=self._prog_lbl=self._speed_lbl=None
        self._xfer_btn=self._cancel_btn=self._log_box=self._sum_frame=None
        self._mode_icon=self._mode_name=self._mode_desc=None
        # monitor widgets
        self._stats={}; self._mon_sub=None
        self._inner=self._mon_canvas=self._mon_win=None
        self._search_var=tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._relayout_grid())
        # status bar
        self._sb_left=self._sb_right=None

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self.after(400, self._refresh_cycle)
        self.after(33,  self._animate)

    # ── full (re)build, used on launch and on theme switch ──
    def _build(self):
        for w in self.winfo_children():
            try: w.destroy()
            except: pass
        self.configure(fg_color=T["bg"])

        # Drop references to the just-destroyed widgets so any callback that
        # fires mid-rebuild (e.g. EndpointPanel._switch -> _on_xfer_change)
        # hits a None guard instead of a dead Tk widget.
        self._from_panel=self._to_panel=None
        self._progress=self._prog_lbl=self._speed_lbl=None
        self._xfer_btn=self._cancel_btn=self._log_box=self._sum_frame=None
        self._mode_icon=self._mode_name=self._mode_desc=None
        self._stats={}; self._mon_sub=None
        self._inner=self._mon_canvas=self._mon_win=None

        self._statusbar = tk.Frame(self, bg=T["panel"], height=26)
        self._statusbar.pack(side="bottom", fill="x"); self._statusbar.pack_propagate(False)
        tk.Frame(self._statusbar, bg=T["border"], height=1).pack(side="top", fill="x")
        self._sb_left = tk.Label(self._statusbar, bg=T["panel"], fg=T["muted"],
                                 font=(MONO,9), anchor="w")
        self._sb_left.pack(side="left", padx=12)
        self._sb_right = tk.Label(self._statusbar, bg=T["panel"], fg=T["dim"],
                                  font=(MONO,9), anchor="e")
        self._sb_right.pack(side="right", padx=12)

        main = tk.Frame(self, bg=T["bg"]); main.pack(side="top", fill="both", expand=True)
        self._rail = tk.Frame(main, bg=T["panel"], width=72)
        self._rail.pack(side="left", fill="y"); self._rail.pack_propagate(False)
        tk.Frame(self._rail, bg=T["border"], width=1).pack(side="right", fill="y")
        self._build_rail()

        self._content = ctk.CTkFrame(main, fg_color=T["bg"], corner_radius=0)
        self._content.pack(side="left", fill="both", expand=True)
        self._mon_frame  = tk.Frame(self._content, bg=T["bg"])
        self._xfer_frame = ctk.CTkFrame(self._content, fg_color=T["bg"], corner_radius=0)
        self._set_frame  = ctk.CTkFrame(self._content, fg_color=T["bg"], corner_radius=0)
        self._build_monitor_view()
        self._build_transfer_view()
        self._build_settings_view()
        self._show_view(self._cur_view)
        self._update_statusbar()

    # ── nav rail ──
    def _build_rail(self):
        logo=tk.Frame(self._rail,bg=T["panel"],height=70); logo.pack(fill="x"); logo.pack_propagate(False)
        tk.Label(logo,text="⬡",bg=T["panel"],fg=T["cyan"],font=(SANS,22,"bold")).pack(pady=(14,0))
        tk.Label(logo,text="OS",bg=T["panel"],fg=T["muted"],font=(MONO,7,"bold")).pack()
        tk.Frame(self._rail,bg=T["border"],height=1).pack(fill="x")

        self._nav_btns={}
        for key,icon,label in (("monitor","◉","Monitor"),
                               ("transfer","⇄","Transfer"),
                               ("settings","⚙","Settings")):
            f=tk.Frame(self._rail,bg=T["panel"],height=64,cursor="hand2"); f.pack(fill="x"); f.pack_propagate(False)
            ico=tk.Label(f,text=icon,bg=T["panel"],fg=T["muted"],font=(SANS,18,"bold")); ico.pack(pady=(12,0))
            txt=tk.Label(f,text=label,bg=T["panel"],fg=T["muted"],font=(MONO,7,"bold")); txt.pack()
            for w in (f,ico,txt):
                w.bind("<Button-1>",lambda e,k=key:self._show_view(k))
                w.bind("<Enter>",lambda e,k=key:self._rail_hover(k,True))
                w.bind("<Leave>",lambda e,k=key:self._rail_hover(k,False))
            self._nav_btns[key]={"frame":f,"icon":ico,"label":txt}

        tk.Frame(self._rail,bg=T["panel"]).pack(fill="y",expand=True)
        tk.Frame(self._rail,bg=T["border"],height=1).pack(fill="x")
        tk.Frame(self._rail,bg=T["border"],height=1).pack(fill="x")
        # theme toggle
        tf=tk.Frame(self._rail,bg=T["panel"],height=52,cursor="hand2"); tf.pack(fill="x"); tf.pack_propagate(False)
        ticon="☀" if CFG["theme"]=="dark" else "☾"
        tl=tk.Label(tf,text=ticon,bg=T["panel"],fg=T["amber"],font=(SANS,15,"bold")); tl.pack(pady=(10,0))
        tk.Label(tf,text="Theme",bg=T["panel"],fg=T["muted"],font=(MONO,7,"bold")).pack()
        for w in (tf,tl): w.bind("<Button-1>",lambda e:self._toggle_theme())
        # reload
        rf=tk.Frame(self._rail,bg=T["panel"],height=52,cursor="hand2"); rf.pack(fill="x"); rf.pack_propagate(False)
        rl=tk.Label(rf,text="⟳",bg=T["panel"],fg=T["muted"],font=(SANS,16,"bold")); rl.pack(pady=(10,0))
        tk.Label(rf,text="Reload",bg=T["panel"],fg=T["muted"],font=(MONO,7,"bold")).pack()
        for w in (rf,rl): w.bind("<Button-1>",lambda e:self._reload_clusters())

    def _rail_hover(self,key,on):
        if self._cur_view==key: return
        c=T["text"] if on else T["muted"]
        self._nav_btns[key]["icon"].config(fg=c); self._nav_btns[key]["label"].config(fg=c)

    def _show_view(self,key):
        self._cur_view=key
        for fr in (self._mon_frame,self._xfer_frame,self._set_frame): fr.pack_forget()
        {"monitor":self._mon_frame,"transfer":self._xfer_frame,
         "settings":self._set_frame}[key].pack(fill="both",expand=True)
        for k,btn in self._nav_btns.items():
            active=(k==key)
            ci=T["cyan"] if active else T["muted"]; cl=T["cyanl"] if active else T["muted"]
            cb=T["card"] if active else T["panel"]
            btn["frame"].config(bg=cb); btn["icon"].config(fg=ci,bg=cb); btn["label"].config(fg=cl,bg=cb)
            for child in btn["frame"].winfo_children():
                if isinstance(child,tk.Frame): 
                    try: child.destroy()
                    except: pass
            if active:
                tk.Frame(btn["frame"],bg=T["cyan"],width=3).place(x=0,y=0,relheight=1.0)
        if key=="monitor": self.after(40,self._relayout_grid)

    def _toggle_theme(self):
        apply_theme("light" if CFG["theme"]=="dark" else "dark")
        save_config()
        self._build()

    def _on_close(self):
        try: self.destroy()
        except Exception: pass

    # ── monitor view ──
    def _build_monitor_view(self):
        mv=self._mon_frame
        hdr=tk.Frame(mv,bg=T["panel"],height=92); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(hdr,bg=T["border"],height=1).place(x=0,rely=1.0,relwidth=1.0,anchor="sw")
        left=tk.Frame(hdr,bg=T["panel"]); left.pack(side="left",padx=22)
        tk.Label(left,text="◉  CLUSTER MONITOR",bg=T["panel"],fg=T["text"],
                 font=(SANS,18,"bold")).pack(anchor="w",pady=(18,0))
        self._mon_sub=tk.Label(left,text="initializing…",bg=T["panel"],fg=T["muted"],
                               font=(MONO,10)); self._mon_sub.pack(anchor="w")

        chips=tk.Frame(hdr,bg=T["panel"]); chips.pack(side="left",padx=24,pady=20)
        for key,label,color in (("total","TOTAL",T["cyan"]),("online","ONLINE",T["green"]),
                                 ("off","OFFLINE",T["red"]),("load","AVG CPU",T["amber"]),
                                 ("sims","SIMS",T["violet"])):
            self._stats[key]=self._stat_card(chips,label,color)

        rp=tk.Frame(hdr,bg=T["panel"]); rp.pack(side="right",padx=18,pady=26)
        self._mk_btn(rp,"⟳ Refresh",T["cyan"],self._refresh_cycle).pack(side="right",padx=4)
        self._mk_btn(rp,"⧉ Open all",T["green"],self._open_all).pack(side="right",padx=4)
        se=tk.Frame(rp,bg=T["card"]); se.pack(side="right",padx=6)
        tk.Label(se,text="⌕",bg=T["card"],fg=T["dim"],font=(SANS,12)).pack(side="left",padx=(8,2))
        tk.Entry(se,textvariable=self._search_var,width=15,bg=T["card"],fg=T["text"],
                 insertbackground=T["text"],relief="flat",font=(MONO,11)).pack(side="left",ipady=5,padx=(0,6))

        wrap=tk.Frame(mv,bg=T["bg"]); wrap.pack(fill="both",expand=True)
        self._mon_canvas=tk.Canvas(wrap,bg=T["bg"],highlightthickness=0)
        self._mon_canvas.pack(side="left",fill="both",expand=True)
        sb=tk.Scrollbar(wrap,orient="vertical",command=self._mon_canvas.yview,
                        bg=T["bg"],troughcolor=T["bg"],bd=0,highlightthickness=0)
        sb.pack(side="right",fill="y"); self._mon_canvas.configure(yscrollcommand=sb.set)
        self._inner=tk.Frame(self._mon_canvas,bg=T["bg"])
        self._mon_win=self._mon_canvas.create_window((0,0),window=self._inner,anchor="nw")
        self._inner.bind("<Configure>",
            lambda e:self._mon_canvas.configure(scrollregion=self._mon_canvas.bbox("all")))
        self._mon_canvas.bind("<Configure>",self._on_canvas_resize)
        for seq,delta in (("<MouseWheel>",None),("<Button-4>",-3),("<Button-5>",3)):
            self._mon_canvas.unbind_all(seq)
        self._mon_canvas.bind_all("<MouseWheel>",
            lambda e:self._mon_canvas.yview_scroll(int(-e.delta/3),"units"))
        self._mon_canvas.bind_all("<Button-4>",lambda e:self._mon_canvas.yview_scroll(-3,"units"))
        self._mon_canvas.bind_all("<Button-5>",lambda e:self._mon_canvas.yview_scroll(3,"units"))

        self._cards.clear(); self._alert_st.clear()
        for r in self._clusters:
            card=HostCard(self._inner,r,on_click=lambda row:open_ssh_terminal(row),
                          on_detail=self._show_slurm_detail,on_transfer=self._launch_transfer_for,
                          on_resize=lambda:self.after(0,self._relayout_grid))
            self._cards[r["hostname"]]=card; self._alert_st[r["hostname"]]=False
        self.after(60,self._relayout_grid)

    def _stat_card(self,parent,label,color):
        f=tk.Frame(parent,bg=T["card"],padx=15,pady=9,highlightbackground=T["border"],
                   highlightthickness=1); f.pack(side="left",padx=5)
        val=tk.Label(f,text="–",bg=T["card"],fg=color,font=(MONO,18,"bold")); val.pack()
        tk.Label(f,text=label,bg=T["card"],fg=T["dim"],font=(MONO,8,"bold")).pack()
        return val

    def _mk_btn(self,parent,text,color,cmd):
        return tk.Button(parent,text=text,command=cmd,relief="flat",bg=T["card"],fg=color,
                         activebackground=T["card2"],activeforeground=color,
                         font=(MONO,11,"bold"),padx=12,pady=6,cursor="hand2",bd=0,highlightthickness=0)

    # ── responsive grid ──
    def _on_canvas_resize(self,e):
        self._mon_canvas.itemconfig(self._mon_win,width=e.width)
        self._relayout_grid()

    def _relayout_grid(self):
        if not self._inner or not self._mon_canvas: return
        W=self._mon_canvas.winfo_width() or 1000
        TILE_MIN=358; GAP=14
        cols=max(1,(W-GAP)//(TILE_MIN+GAP))
        q=self._search_var.get().lower().strip()
        visible=[(h,c) for h,c in self._cards.items()
                 if not q or q in h.lower() or q in c.row["ip"].lower()]
        for _,c in self._cards.items(): c.cv.grid_forget()
        for i in range(20): self._inner.grid_columnconfigure(i,weight=0)
        for i in range(cols): self._inner.grid_columnconfigure(i,weight=1,uniform="cards")
        for idx,(h,c) in enumerate(visible):
            r,cc=divmod(idx,cols)
            c.cv.grid(row=r,column=cc,sticky="new",padx=GAP//2,pady=GAP//2)
        self._grid_cols=cols

    # ── monitor logic ──
    def _refresh_cycle(self):
        if self._mon_sub: self._mon_sub.config(text="scanning nodes…")
        delay=0
        for host,card in self._cards.items():
            self.after(delay,lambda h=host,r=dict(card.row):self._pool.submit(self._probe,h,r))
            delay+=60   # stagger to avoid connection bursts
        self.after(CFG["refresh_time"]*1000,self._refresh_cycle)
        self.after(1500,self._update_summary)

    def _probe(self,host,r):
        online=is_online(r)
        st=get_remote_status(r) if online else None
        self.after(0,self._apply,host,online,st)
        # Slurm is shown when pinned in SLURM_HOSTS OR auto-detected on the host.
        detected = bool(st and str(st.get("has_slurm","0"))=="1")
        if online and (host in SLURM_HOSTS or detected):
            sl=get_slurm_status(r,slurm_cfg_for(host,r))
            self.after(0,self._apply_slurm,host,sl)

    def _apply(self,host,online,st):
        card=self._cards.get(host)
        if not card: return
        if not online:
            card.set_data(False,"OFFLINE",None)
            self._maybe_alert(host,"OFFLINE",f"{host} unreachable")
        elif st is None:
            card.set_data(True,"SSH ERROR",None)
        else:
            card.set_data(True,"ONLINE",st)
            card.set_slurm_capable(host in SLURM_HOSTS or str(st.get("has_slurm","0"))=="1")
            cpu,ram=to_float(st["cpu"]),to_float(st["ram"])
            if (cpu and cpu>=CFG["cpu_alert"]) or (ram and ram>=CFG["ram_alert"]):
                self._maybe_alert(host,"HIGH LOAD",f"{host}: CPU {cpu}% RAM {ram}%")
            else: self._alert_st[host]=False
        card.draw(); self._update_summary()

    def _apply_slurm(self,host,sl):
        card=self._cards.get(host)
        if card and sl:
            card.set_slurm_capable(True)
            card.set_slurm(sl)
            lim=sl.get("core_limit",0)
            if lim and sl.get("cores_used",0)>=lim:
                self._maybe_alert(host,"CORE LIMIT",
                                  f"{host}: {sl['cores_used']}/{lim} cores")

    def _update_summary(self):
        total=len(self._cards)
        online=sum(1 for c in self._cards.values() if c.status.startswith("ONLINE"))
        off=sum(1 for c in self._cards.values() if "OFFLINE" in c.status)
        loads=[c.target["cpu"] for c in self._cards.values() if c.status.startswith("ONLINE")]
        avg=sum(loads)/len(loads) if loads else 0
        sims=sum(int(c.meta.get("sim_total","0") or 0)
                 for c in self._cards.values() if c.online and c.meta)
        m={"total":str(total),"online":str(online),"off":str(off),
           "load":f"{avg:.0f}%","sims":str(sims)}
        for k,v in m.items():
            if self._stats.get(k): self._stats[k].config(text=v)
        if self._mon_sub:
            self._mon_sub.config(text=f"updated {time.strftime('%H:%M:%S')}  •  "
                                      f"refresh {CFG['refresh_time']}s  •  {sims} sims")
        self._update_statusbar()

    def _maybe_alert(self,host,kind,msg):
        if not self._alert_st.get(host):
            notify(f"Cluster {kind}",msg); self._alert_st[host]=True

    def _animate(self):
        for c in self._cards.values():
            if c.ease(): c.draw()
        self.after(33,self._animate)

    def _open_all(self):
        d=0
        for card in self._cards.values():
            self.after(d,lambda r=card.row:open_ssh_terminal(r)); d+=600

    def _update_statusbar(self):
        if not self._sb_left: return
        self._sb_left.config(text=f"⬡ ClusterOS   ·   {len(self._cards)} clusters   ·   "
                                  f"theme {CFG['theme']}")
        self._sb_right.config(text=f"refresh {CFG['refresh_time']}s   ·   "
                                   f"probe×{CFG['monitor_workers']}   ·   "
                                   f"sftp×{CFG['sftp_workers']}   ·   "
                                   f"retries {CFG['connect_retries']}")

    # ── slurm detail popup ──
    def _show_slurm_detail(self,row):
        host=row["hostname"]; cfg=slurm_cfg_for(host,row)
        win=tk.Toplevel(self); win.title(f"{host} — Slurm Detail")
        win.configure(bg=T["bg"]); win.geometry("960x820")
        hdr=tk.Frame(win,bg=T["panel"],height=64); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Label(hdr,text=f"⊞  {host}",bg=T["panel"],fg=T["text"],font=(SANS,17,"bold")).pack(side="left",padx=20,pady=18)
        sub=tk.Label(hdr,text="loading…",bg=T["panel"],fg=T["muted"],font=(MONO,10)); sub.pack(side="left",padx=4,pady=20)
        body=tk.Frame(win,bg=T["bg"]); body.pack(fill="both",expand=True,padx=18,pady=14)
        usage=tk.Canvas(body,height=96,bg=T["bg"],highlightthickness=0); usage.pack(fill="x")
        tk.Label(body,text="COMPUTE NODE AVAILABILITY",bg=T["bg"],fg=T["cyan"],font=(MONO,10,"bold")).pack(anchor="w",pady=(10,4))
        nf=tk.Frame(body,bg=T["bg"],height=190); nf.pack(fill="x"); nf.pack_propagate(False)
        ncv=tk.Canvas(nf,bg=T["bg"],highlightthickness=0)
        nsb=tk.Scrollbar(nf,orient="vertical",command=ncv.yview,bg=T["bg"],troughcolor=T["card"],bd=0)
        ncv.configure(yscrollcommand=nsb.set); ncv.pack(side="left",fill="both",expand=True); nsb.pack(side="right",fill="y")
        tk.Label(body,text="YOUR JOBS",bg=T["bg"],fg=T["cyan"],font=(MONO,10,"bold")).pack(anchor="w",pady=(12,4))
        jobs_txt=tk.Text(body,bg=T["card"],fg=T["text"],relief="flat",font=(MONO,10),height=9,wrap="none",insertbackground=T["text"]); jobs_txt.pack(fill="both",expand=True)
        tk.Label(body,text="PARTITION STATES",bg=T["bg"],fg=T["cyan"],font=(MONO,10,"bold")).pack(anchor="w",pady=(10,4))
        part_txt=tk.Text(body,bg=T["card"],fg=T["muted"],relief="flat",font=(MONO,10),height=4,wrap="none"); part_txt.pack(fill="x")
        btns=tk.Frame(win,bg=T["bg"]); btns.pack(fill="x",pady=10)
        self._mk_btn(btns,"⟳ Refresh",T["cyan"],lambda:self._pool.submit(load)).pack(side="right",padx=14)
        self._mk_btn(btns,"⧉ SSH in",T["green"],lambda:open_ssh_terminal(row)).pack(side="right")

        def render(sl):
            if not win.winfo_exists(): return
            usage.delete("all")
            if not sl: sub.config(text="could not query Slurm"); return
            used   = sl.get("cores_used",0)            # cores in YOUR running jobs
            limit  = sl.get("core_limit",0)            # per-user CORE cap (0 = none)
            cl_tot = sl.get("cluster_cores_total",0)
            cl_busy= sl.get("cluster_cores_alloc",0)
            nodes_t= sl.get("nodes_total",0)
            sub.config(text=f"user {sl['user']}  •  {sl['running']} running  •  "
                            f"{sl['pending']} pending  •  using {sl.get('nodes_used',0)} node(s)")
            W=usage.winfo_width() or 800

            # ── Row 1: your per-user CORE allocation against the per-user cap ──
            if limit:
                pct=min(100,used/limit*100)
                col=T["red"] if pct>=90 else (T["amber"] if pct>=70 else T["violet"])
                src="(per-user core limit)" if sl.get("detected_limit") or limit else ""
                usage.create_text(6,14,anchor="w",fill=T["muted"],font=(MONO,10,"bold"),
                                  text=f"YOUR CORES   {used} / {limit}")
                usage.create_text(160,14,anchor="w",fill=T["dim"],font=(MONO,8),text=src)
                usage.create_text(W-6,14,anchor="e",fill=col,font=(MONO,11,"bold"),text=f"{pct:.0f}%")
                HostCard._rr(usage,4,24,W-4,40,8,fill=T["track"],outline="")
                fw=min(used/limit,1.0)*(W-8)
                if fw>4: HostCard._rr(usage,4,24,4+fw,40,8,fill=col,outline="")
            else:
                usage.create_text(6,14,anchor="w",fill=T["muted"],font=(MONO,10,"bold"),
                                  text=f"YOUR CORES   {used} in use")
                usage.create_text(W-6,14,anchor="e",fill=T["dim"],font=(MONO,8),
                                  text="(no per-user cap set)")
                HostCard._rr(usage,4,24,W-4,40,8,fill=T["track"],outline="")
                if cl_tot:
                    fw=min(used/cl_tot,1.0)*(W-8)
                    if fw>4: HostCard._rr(usage,4,24,4+fw,40,8,fill=T["violet"],outline="")

            # ── Row 2: whole-cluster capacity (total cores across all nodes) ──
            if cl_tot:
                cpct=min(100,cl_busy/cl_tot*100)
                ccol=T["red"] if cpct>=90 else (T["amber"] if cpct>=70 else T["green"])
                usage.create_text(6,58,anchor="w",fill=T["muted"],font=(MONO,9,"bold"),
                                  text=f"CLUSTER   {cl_busy} / {cl_tot} cores busy   "
                                       f"· {nodes_t} nodes")
                usage.create_text(W-6,58,anchor="e",fill=ccol,font=(MONO,10,"bold"),text=f"{cpct:.0f}%")
                HostCard._rr(usage,4,66,W-4,80,7,fill=T["track"],outline="")
                fw=min(cl_busy/cl_tot,1.0)*(W-8)
                if fw>4: HostCard._rr(usage,4,66,4+fw,80,7,fill=ccol,outline="")
            ncv.delete("all"); nodelist=sl.get("nodelist",[])
            if nodelist:
                W2=ncv.winfo_width() or 880; cols=3; cw,ch=(W2-20)//cols,80; gap=8
                rows_n=(len(nodelist)+cols-1)//cols; total_h=rows_n*(ch+gap)+16
                for i,node in enumerate(nodelist):
                    ri,ci=divmod(i,cols); x=10+ci*cw; y=8+ri*(ch+gap)
                    state=node["state"].lower(); alloc,idle,tot=node["cores_alloc"],node["cores_idle"],node["cores_total"]
                    if "down" in state or "drain" in state: bg2,fg2,stxt=T["dim"],T["muted"],"DOWN"
                    elif idle==tot: bg2,fg2,stxt=T["green"],T["text"],"IDLE"
                    elif idle==0:   bg2,fg2,stxt=T["red"],T["text"],"FULL"
                    else:           bg2,fg2,stxt=T["amber"],T["text"],"MIX"
                    HostCard._rr(ncv,x,y,x+cw-12,y+ch-8,8,fill=T["card"],outline=bg2)
                    ncv.create_text(x+8,y+12,anchor="w",fill=fg2,font=(MONO,10,"bold"),text=node["name"])
                    ncv.create_text(x+cw-16,y+12,anchor="e",fill=bg2,font=(MONO,8,"bold"),text=stxt)
                    bx2,by2,bw2=x+8,y+32,cw-32
                    HostCard._rr(ncv,bx2,by2,bx2+bw2,by2+8,4,fill=T["track"],outline="")
                    if tot>0 and alloc>0:
                        HostCard._rr(ncv,bx2,by2,bx2+(alloc/tot)*bw2,by2+8,4,fill=bg2,outline="")
                    ncv.create_text(x+8,y+52,anchor="w",fill=T["dim"],font=(MONO,8),text=f"{idle} idle / {tot} cores")
                    if node.get("gres") and node["gres"]!="none":
                        ncv.create_text(x+8,y+64,anchor="w",fill=T["violet"],font=(MONO,7),text=f"GPU: {node['gres']}")
                ncv.configure(scrollregion=(0,0,W2,total_h))
            else:
                ncv.create_text(10,20,anchor="w",fill=T["dim"],font=(MONO,10),text="(no per-node data)")
                ncv.configure(scrollregion=(0,0,100,50))
            jobs_txt.delete("1.0","end")
            jobs_txt.insert("end",f"{'JOBID':<10}{'NAME':<16}{'STATE':<10}{'NODES':>6}{'CORES':>7}  TIME\n")
            jobs_txt.insert("end","─"*60+"\n")
            if sl["jobs"]:
                for j in sl["jobs"]:
                    jobs_txt.insert("end",f"{j['id']:<10}{j['name'][:15]:<16}{j['state']:<10}{j['nodes']:>6}{j['cores']:>7}  {j['time']}\n")
            else: jobs_txt.insert("end","  (no jobs in queue)\n")
            part_txt.delete("1.0","end")
            for pname,states in sl.get("part_states",{}).items():
                part_txt.insert("end",f"{pname:<16} "+"  ".join(f"{k}:{v}" for k,v in states.items())+"\n")

        def load():
            sl=get_slurm_status(row,cfg); self.after(0,lambda:render(sl))
        cached=self._cards[host].slurm
        if cached: win.after(80,lambda:render(cached))
        self._pool.submit(load)

    def _launch_transfer_for(self,row):
        self._show_view("transfer")
        if self._from_panel:
            self._from_panel.prefill_cluster(row); self._on_xfer_change()

    # ── transfer view ──
    def _build_transfer_view(self):
        xv=self._xfer_frame
        xv.grid_rowconfigure(1,weight=1); xv.grid_columnconfigure(0,weight=1)

        hdr=ctk.CTkFrame(xv,fg_color=T["panel"],corner_radius=0,height=64); hdr.grid(row=0,column=0,sticky="ew"); hdr.grid_propagate(False)
        ctk.CTkFrame(hdr,fg_color=T["border"],height=1,corner_radius=0).place(x=0,rely=1.0,relwidth=1.0,anchor="sw")
        ctk.CTkLabel(hdr,text="⇄  File Transfer",font=F(18,True),text_color=T["text"]).pack(side="left",padx=24,pady=18)
        ctk.CTkLabel(hdr,text=f"Pooled SFTP ×{CFG['sftp_workers']}  ·  retry-safe  ·  auto-mode",
                     font=FM(10),text_color=T["dim"]).pack(side="left")
        self._speed_lbl=ctk.CTkLabel(hdr,text="",font=FM(10),text_color=T["muted"]); self._speed_lbl.pack(side="right",padx=20)

        bar=ctk.CTkFrame(xv,fg_color=T["panel"],corner_radius=0,height=58); bar.grid(row=2,column=0,sticky="ew"); bar.grid_propagate(False)
        ctk.CTkFrame(bar,fg_color=T["border"],height=1,corner_radius=0).place(x=0,y=0,relwidth=1.0)
        sf=ctk.CTkFrame(bar,fg_color="transparent"); sf.pack(side="left",fill="x",expand=True,padx=(20,10),pady=12)
        self._prog_lbl=ctk.CTkLabel(sf,text="Ready",font=F(11),text_color=T["muted"],anchor="w"); self._prog_lbl.pack(anchor="w")
        self._progress=ctk.CTkProgressBar(sf,height=4,corner_radius=2,fg_color=T["raised"],progress_color=T["cyan"])
        self._progress.pack(fill="x",pady=(4,0)); self._progress.set(0)
        self._cancel_btn=ctk.CTkButton(bar,text="✕ Cancel",width=104,height=36,fg_color=T["raised"],
            hover_color=T["red"],text_color=T["muted"],font=F(12),corner_radius=8,command=self._cancel_xfer,state="disabled")
        self._cancel_btn.pack(side="right",padx=(0,14),pady=11)
        self._xfer_btn=ctk.CTkButton(bar,text="▶   Transfer",width=158,height=36,fg_color=T["cyan"],
            hover_color=T["cyanh"],text_color=T["void"],font=F(13,True),corner_radius=8,command=self._start_xfer)
        self._xfer_btn.pack(side="right",padx=(0,6),pady=11)

        body=ctk.CTkFrame(xv,fg_color="transparent",corner_radius=0)
        body.grid(row=1,column=0,sticky="nsew",padx=18,pady=14)
        body.grid_rowconfigure(0,weight=1)
        body.grid_columnconfigure(0,weight=1)            # left expands
        body.grid_columnconfigure(1,minsize=320,weight=0) # right fixed
        left=ctk.CTkScrollableFrame(body,fg_color="transparent",
            scrollbar_button_color=T["border2"],scrollbar_button_hover_color=T["border"])
        left.grid(row=0,column=0,sticky="nsew",padx=(0,12))
        right=ctk.CTkFrame(body,fg_color="transparent",width=320)
        right.grid(row=0,column=1,sticky="nsew"); right.grid_propagate(False)
        self._build_endpoints(left); self._build_xfer_right(right)

    def _build_endpoints(self,parent):
        wrap=ctk.CTkFrame(parent,fg_color="transparent"); wrap.pack(fill="x",padx=2,pady=(2,10))
        self._from_panel=EndpointPanel(wrap,"FROM",T["cyan"],self._clusters,on_change=self._on_xfer_change)
        self._from_panel.pack(fill="x")
        arr=ctk.CTkFrame(wrap,fg_color="transparent",height=36); arr.pack(fill="x"); arr.pack_propagate(False)
        ctk.CTkFrame(arr,fg_color=T["border2"],height=1).place(relx=0.0,rely=0.5,relwidth=0.44,anchor="w")
        ctk.CTkLabel(arr,text="▼",font=F(14),text_color=T["dim"]).place(relx=0.5,rely=0.5,anchor="center")
        ctk.CTkFrame(arr,fg_color=T["border2"],height=1).place(relx=1.0,rely=0.5,relwidth=0.44,anchor="e")
        self._to_panel=EndpointPanel(wrap,"TO",T["green"],self._clusters,on_change=self._on_xfer_change)
        self._to_panel._switch("cluster"); self._to_panel.pack(fill="x")

    def _build_xfer_right(self,parent):
        mc=ctk.CTkFrame(parent,fg_color=T["card"],corner_radius=14,border_width=1,border_color=T["border"]); mc.pack(fill="x",pady=(0,12))
        mh=ctk.CTkFrame(mc,fg_color=T["card2"],corner_radius=0,height=38); mh.pack(fill="x"); mh.pack_propagate(False)
        ctk.CTkFrame(mh,fg_color=T["cyan"],width=3,corner_radius=0).pack(side="left",fill="y")
        ctk.CTkLabel(mh,text="  MODE",font=F(8,True),text_color=T["void"],fg_color=T["cyan"],corner_radius=0).pack(side="left",padx=(8,10),pady=11)
        ctk.CTkLabel(mh,text="Transfer Mode",font=F(12,True),text_color=T["text"]).pack(side="left")
        mi=ctk.CTkFrame(mc,fg_color="transparent"); mi.pack(fill="x",padx=14,pady=12)
        r0=ctk.CTkFrame(mi,fg_color="transparent"); r0.pack(fill="x")
        self._mode_icon=ctk.CTkLabel(r0,text="⬆",font=F(20,True),text_color=T["cyan"],width=30); self._mode_icon.pack(side="left",padx=(0,8))
        self._mode_name=ctk.CTkLabel(r0,text="Upload",font=F(14,True),text_color=T["text"],anchor="w"); self._mode_name.pack(side="left",fill="x",expand=True)
        self._mode_desc=ctk.CTkLabel(mi,text="Local  →  Cluster",font=FM(10),text_color=T["muted"],anchor="w"); self._mode_desc.pack(anchor="w",pady=(4,0))

        sc=ctk.CTkFrame(parent,fg_color=T["card"],corner_radius=14,border_width=1,border_color=T["border"]); sc.pack(fill="x",pady=(0,12))
        sh=ctk.CTkFrame(sc,fg_color=T["card2"],corner_radius=0,height=38); sh.pack(fill="x"); sh.pack_propagate(False)
        ctk.CTkFrame(sh,fg_color=T["amber"],width=3,corner_radius=0).pack(side="left",fill="y")
        ctk.CTkLabel(sh,text="  SUMMARY",font=F(8,True),text_color=T["void"],fg_color=T["amber"],corner_radius=0).pack(side="left",padx=(8,10),pady=11)
        ctk.CTkLabel(sh,text="Transfer Summary",font=F(12,True),text_color=T["text"]).pack(side="left")
        self._sum_frame=ctk.CTkFrame(sc,fg_color="transparent"); self._sum_frame.pack(fill="x",padx=14,pady=12)
        self._refresh_summary()

        lc=ctk.CTkFrame(parent,fg_color=T["card"],corner_radius=14,border_width=1,border_color=T["border"]); lc.pack(fill="both",expand=True)
        lh=ctk.CTkFrame(lc,fg_color=T["card2"],corner_radius=0,height=38); lh.pack(fill="x"); lh.pack_propagate(False)
        ctk.CTkFrame(lh,fg_color=T["green"],width=3,corner_radius=0).pack(side="left",fill="y")
        ctk.CTkLabel(lh,text="  LOG",font=F(8,True),text_color=T["void"],fg_color=T["green"],corner_radius=0).pack(side="left",padx=(8,10),pady=11)
        ctk.CTkLabel(lh,text="Activity Log",font=F(12,True),text_color=T["text"]).pack(side="left")
        ctk.CTkButton(lh,text="Clear",width=50,height=24,fg_color="transparent",hover_color=T["border2"],
            font=F(10),text_color=T["dim"],corner_radius=6,command=self._clear_log).pack(side="right",padx=10,pady=7)
        self._log_box=ctk.CTkTextbox(lc,font=FM(11),fg_color=T["card"],text_color=T["muted"],border_width=0,wrap="none")
        self._log_box.pack(fill="both",expand=True,padx=2,pady=(0,2))
        self._log(f"Ready  ·  {len(self._clusters)} cluster(s) loaded","ok")

    _MODES={"upload":("⬆","Upload","Local  →  Cluster"),
            "download":("⬇","Download","Cluster  →  Local"),
            "cluster2cluster":("⇄","Cluster Transfer","Cluster  →  Cluster"),
            "local2local":("⇌","Local Copy","Local  →  Local")}

    def _detect_mode(self):
        if not self._from_panel or not self._to_panel: return "upload"
        ft,tt=self._from_panel.get_type(),self._to_panel.get_type()
        if   ft=="local"   and tt=="cluster": return "upload"
        elif ft=="cluster" and tt=="local":   return "download"
        elif ft=="cluster" and tt=="cluster": return "cluster2cluster"
        else:                                  return "local2local"

    def _on_xfer_change(self):
        if self._mode_icon:
            ico,name,desc=self._MODES.get(self._detect_mode(),("?","Unknown",""))
            self._mode_icon.configure(text=ico); self._mode_name.configure(text=name); self._mode_desc.configure(text=desc)
        self._refresh_summary()

    def _refresh_summary(self):
        if not self._sum_frame: return
        try:
            for w in self._sum_frame.winfo_children(): w.destroy()
        except: return
        ico,name,_=self._MODES.get(self._detect_mode(),("?","Unknown",""))
        def row(k,v,vc=None,mono=False):
            vc=vc or T["muted"]
            r=ctk.CTkFrame(self._sum_frame,fg_color="transparent"); r.pack(fill="x",pady=1)
            ctk.CTkLabel(r,text=k,font=F(9,True),text_color=T["dim"],width=60,anchor="w").pack(side="left")
            disp=(v[:30]+"…") if len(v)>32 else v
            ctk.CTkLabel(r,text=disp,font=FM(10) if mono else F(10),text_color=vc,anchor="w").pack(side="left",fill="x",expand=True)
        def div(): ctk.CTkFrame(self._sum_frame,fg_color=T["border"],height=1).pack(fill="x",pady=5)
        row("Mode",f"{ico}  {name}",T["cyanl"]); div()
        if self._from_panel:
            ft=self._from_panel.get_type(); fp=self._from_panel.get_path() or "—"
            if ft=="cluster":
                fc=self._from_panel.get_cluster()
                row("From",fc["hostname"] if fc else "—",T["text"]); row("IP",fc["ip"] if fc else "—",T["cyanl"],True); row("Path",fp,T["green"],True)
            else: row("From","Local",T["text"]); row("Path",fp,T["amber"],True)
        div()
        if self._to_panel:
            tt=self._to_panel.get_type(); tp=self._to_panel.get_path() or "—"
            if tt=="cluster":
                tc=self._to_panel.get_cluster()
                row("To",tc["hostname"] if tc else "—",T["text"]); row("IP",tc["ip"] if tc else "—",T["cyanl"],True); row("Path",tp,T["green"],True)
            else: row("To","Local",T["text"]); row("Path",tp,T["amber"],True)

    def _log(self,msg,level="info"):
        if not self._log_box: return
        ts=datetime.now().strftime("%H:%M:%S")
        icon={"info":"·","ok":"✓","error":"✗","warn":"⚠"}.get(level,"·")
        self._log_box.configure(state="normal"); self._log_box.insert("end",f"  {ts}  {icon}  {msg}\n")
        self._log_box.see("end"); self._log_box.configure(state="disabled")

    def _clear_log(self):
        if not self._log_box: return
        self._log_box.configure(state="normal"); self._log_box.delete("1.0","end"); self._log_box.configure(state="disabled")

    def _set_progress(self,v,lbl=""):
        if self._progress: self._progress.set(v)
        if lbl and self._prog_lbl: self._prog_lbl.configure(text=lbl)
    def _set_speed(self,s):
        if self._speed_lbl: self._speed_lbl.configure(text=s)

    def _start_xfer(self):
        mode=self._detect_mode()
        fpath,tpath=self._from_panel.get_path(),self._to_panel.get_path()
        fc,tc=self._from_panel.get_cluster(),self._to_panel.get_cluster()
        if mode=="local2local": messagebox.showwarning("Both local","Set at least one endpoint to Cluster."); return
        if not fpath: messagebox.showwarning("Missing","No FROM path selected."); return
        if not tpath: messagebox.showwarning("Missing","No TO path selected."); return
        if mode=="cluster2cluster" and fc and tc and fc["hostname"]==tc["hostname"]:
            messagebox.showwarning("Same cluster","FROM and TO are the same cluster."); return
        self._cancel_flag.clear(); self._xfer_start=time.time()
        self._xfer_btn.configure(state="disabled"); self._cancel_btn.configure(state="normal")
        self._speed_lbl.configure(text=""); self._set_progress(0,"Connecting…")
        threading.Thread(target=self._run_xfer,args=(mode,fc,tc,fpath,tpath),daemon=True).start()

    def _cancel_xfer(self):
        self._cancel_flag.set(); self._log("Transfer cancelled.","warn"); self.after(0,self._reset_xfer_ui)

    def _reset_xfer_ui(self):
        if self._xfer_btn: self._xfer_btn.configure(state="normal")
        if self._cancel_btn: self._cancel_btn.configure(state="disabled")
        self._set_progress(0,"Ready")
        if self._speed_lbl: self._speed_lbl.configure(text="")

    def _run_xfer(self,mode,fc,tc,fpath,tpath):
        try:
            if   mode=="upload":          self._do_upload(tc,fpath,tpath)
            elif mode=="download":        self._do_download(fc,fpath,tpath)
            elif mode=="cluster2cluster": self._do_c2c(fc,tc,fpath,tpath)
        except Exception as e:
            self._log(f"Error: {e}","error"); self.after(0,lambda:self._set_progress(0,"Failed"))
        finally:
            self.after(0,self._reset_xfer_ui)

    # ── transfer helpers ──
    def _ssh_connect(self,r):
        self._log(f"Connecting → {r['hostname']} ({r['ip']})…")
        ssh=connect_ssh(r)
        self._log(f"Connected ✓  {r['hostname']}","ok")
        return ssh

    def _remote_exists(self,sftp,path):
        try: sftp.stat(path); return True
        except: return False
    def _remote_is_dir(self,sftp,path):
        try: return stat.S_ISDIR(sftp.stat(path).st_mode)
        except: return False
    def _sftp_makedirs(self,sftp,rp):
        cur=""
        for part in rp.replace("\\","/").split("/"):
            if not part: cur="/"; continue
            cur=cur.rstrip("/")+"/"+part
            if not self._remote_exists(sftp,cur):
                try: sftp.mkdir(cur)
                except Exception as e: self._log(f"  mkdir {cur}: {e}","warn")

    def _prog_cb(self,transferred,total):
        elapsed=max(time.time()-self._xfer_start,0.001); bps=transferred/elapsed
        speed=f"{bps/1e6:.1f} MB/s" if bps>=1e6 else (f"{bps/1e3:.0f} KB/s" if bps>=1e3 else f"{bps:.0f} B/s")
        if total>0:
            pct=transferred/total
            info=f"Transferring…  {human_size(transferred)} / {human_size(total)}"
            self.after(0,lambda p=pct,t=info:self._set_progress(0.2+0.78*p,t))
        self.after(0,lambda s=speed:self._set_speed(s))

    # ── throttle-safe parallel transfer core ──
    def _pooled(self,cluster,total,do_one,verb):
        """Run do_one(sftp, pair) across sftp_workers, reusing ONE connection
        per worker thread (not per file). Collapses N handshakes to ~workers."""
        workers=max(1,CFG["sftp_workers"])
        tls=threading.local(); conns=[]; clock=threading.Lock()
        hs=threading.Semaphore(workers)        # stagger concurrent handshakes
        lock=threading.Lock(); done=[0]; self._xfer_start=time.time()
        def get_sftp():
            if not getattr(tls,"sftp",None):
                with hs:
                    ssh,sftp=sftp_connect(cluster)
                tls.ssh,tls.sftp=ssh,sftp
                with clock: conns.append((ssh,sftp))
            return tls.sftp
        def worker(pair):
            if self._cancel_flag.is_set(): return
            do_one(get_sftp(),pair)
            with lock:
                done[0]+=1; pct=0.15+0.80*(done[0]/total)
                el=max(time.time()-self._xfer_start,0.001)
                self.after(0,lambda p=pct,d=done[0]:self._set_progress(p,f"{verb} {d}/{total} files…"))
                self.after(0,lambda d=done[0],e=el:self._set_speed(f"{d}/{total} files · {e:.0f}s"))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs=[pool.submit(worker,p) for p in self._pairs_iter]
                for f in as_completed(futs):
                    if self._cancel_flag.is_set(): break
                    try: f.result()
                    except Exception as e: self._log(f"  {verb} err: {e}","warn")
        finally:
            for ssh,sftp in conns:
                try: sftp.close()
                except: pass
                try: ssh.close()
                except: pass

    # ── upload ──
    def _do_upload(self,cluster,local,remote):
        self._log(f"Upload  {os.path.basename(local)}  →  {cluster['hostname']}:{remote}")
        self.after(0,lambda:self._set_progress(0.05,"Authenticating…"))
        ssh=self._ssh_connect(cluster)
        if self._cancel_flag.is_set(): ssh.close(); return
        sftp=ssh.open_sftp()
        try:
            if os.path.isfile(local):
                if self._remote_is_dir(sftp,remote):
                    dest=remote.rstrip("/")+"/"+os.path.basename(local)
                else:
                    self._sftp_makedirs(sftp,remote.rsplit("/",1)[0] if "/" in remote else "/"); dest=remote
                self._log(f"  → {dest}"); self.after(0,lambda:self._set_progress(0.2,"Uploading file…"))
                self._xfer_start=time.time(); sftp.put(local,dest,callback=self._prog_cb)
            elif os.path.isdir(local):
                pairs=[(os.path.join(root,fn),
                        os.path.relpath(os.path.join(root,fn),os.path.dirname(local)).replace("\\","/"))
                       for root,_,files in os.walk(local) for fn in files]
                total=max(len(pairs),1)
                self._log(f"  Folder: {total} files  [pooled ×{CFG['sftp_workers']}]")
                self.after(0,lambda:self._set_progress(0.15,"Uploading folder…"))
                self._sftp_makedirs(sftp,remote); sftp.close(); ssh.close(); sftp=ssh=None
                def one(s,pair):
                    lp,rel=pair; rp=remote.rstrip("/")+"/"+rel
                    self._sftp_makedirs(s,rp.rsplit("/",1)[0]); s.put(lp,rp); self._log(f"  ↑  {rel}")
                self._pairs_iter=pairs; self._pooled(cluster,total,one,"Uploading")
            else:
                raise FileNotFoundError(f"Not found: {local}")
        finally:
            if sftp:
                try: sftp.close()
                except: pass
            if ssh:
                try: ssh.close()
                except: pass
        if not self._cancel_flag.is_set():
            self._log("Upload complete.","ok"); self.after(0,lambda:self._set_progress(1.0,"Done ✓"))

    # ── download ──
    def _collect_remote(self,sftp,rdir):
        pairs=[]
        def walk(d):
            try: entries=sftp.listdir_attr(d)
            except: return
            for e in entries:
                rp=d.rstrip("/")+"/"+e.filename
                if stat.S_ISDIR(e.st_mode): walk(rp)
                else:
                    rel=rp[len(os.path.dirname(rdir.rstrip("/"))):].lstrip("/"); pairs.append((rp,rel))
        walk(rdir); return pairs

    def _do_download(self,cluster,remote,local):
        self._log(f"Download  {cluster['hostname']}:{remote}  →  {local}")
        self.after(0,lambda:self._set_progress(0.05,"Authenticating…"))
        ssh=self._ssh_connect(cluster)
        if self._cancel_flag.is_set(): ssh.close(); return
        sftp=ssh.open_sftp()
        try:
            if self._remote_is_dir(sftp,remote):
                self.after(0,lambda:self._set_progress(0.12,"Scanning remote…"))
                pairs=self._collect_remote(sftp,remote); total=max(len(pairs),1)
                self._log(f"  Folder: {total} files  [pooled ×{CFG['sftp_workers']}]")
                os.makedirs(local,exist_ok=True); sftp.close(); ssh.close(); sftp=ssh=None
                self.after(0,lambda:self._set_progress(0.15,"Downloading folder…"))
                def one(s,pair):
                    rp,rel=pair; lp=os.path.join(local,rel.replace("/",os.sep))
                    os.makedirs(os.path.dirname(lp),exist_ok=True); s.get(rp,lp); self._log(f"  ↓  {rel}")
                self._pairs_iter=pairs; self._pooled(cluster,total,one,"Downloading")
            else:
                fname=os.path.basename(remote.rstrip("/"))
                dest=os.path.join(local,fname) if os.path.isdir(local) else local
                os.makedirs(os.path.dirname(dest) or ".",exist_ok=True)
                self._log(f"  → {dest}"); self.after(0,lambda:self._set_progress(0.2,"Downloading file…"))
                self._xfer_start=time.time(); sftp.get(remote,dest,callback=self._prog_cb)
        finally:
            if sftp:
                try: sftp.close()
                except: pass
            if ssh:
                try: ssh.close()
                except: pass
        if not self._cancel_flag.is_set():
            self._log("Download complete.","ok"); self.after(0,lambda:self._set_progress(1.0,"Done ✓"))

    # ── cluster-to-cluster ──
    def _do_c2c(self,src,dst,spath,dpath):
        self._log(f"C2C  {src['hostname']}:{spath}  →  {dst['hostname']}:{dpath}")
        self.after(0,lambda:self._set_progress(0.1,"Connecting to source…"))
        ssh=self._ssh_connect(src)
        if self._cancel_flag.is_set(): ssh.close(); return
        self.after(0,lambda:self._set_progress(0.4,"Running transfer…"))
        _,chk,_=ssh.exec_command("which rsync"); has_rsync=bool(chk.read().decode().strip())
        if has_rsync:
            cmd=(f"rsync -az --progress "
                 f"-e \"sshpass -p '{dst['password']}' ssh -o StrictHostKeyChecking=no\" "
                 f"'{spath}' '{dst['username']}@{dst['ip']}:{dpath}' 2>&1 | tail -5"); tool="rsync"
        else:
            cmd=(f"sshpass -p '{dst['password']}' scp -o StrictHostKeyChecking=no -r "
                 f"'{spath}' '{dst['username']}@{dst['ip']}:{dpath}'"); tool="scp"
        self._log(f"  Using {tool}")
        _,stdout,stderr=ssh.exec_command(cmd,timeout=600)
        out=stdout.read().decode().strip(); err=stderr.read().decode().strip()
        code=stdout.channel.recv_exit_status(); ssh.close()
        if out: self._log(out)
        if err: self._log(err,"warn")
        if code==0:
            self._log("C2C transfer complete.","ok"); self.after(0,lambda:self._set_progress(1.0,"Done ✓"))
        else:
            self._log(f"{tool} exited {code}","error"); self.after(0,lambda:self._set_progress(0,"Failed"))

    # ── settings view ──
    def _build_settings_view(self):
        sv=self._set_frame
        sv.grid_rowconfigure(1,weight=1); sv.grid_columnconfigure(0,weight=1)
        hdr=ctk.CTkFrame(sv,fg_color=T["panel"],corner_radius=0,height=64); hdr.grid(row=0,column=0,sticky="ew"); hdr.grid_propagate(False)
        ctk.CTkFrame(hdr,fg_color=T["border"],height=1,corner_radius=0).place(x=0,rely=1.0,relwidth=1.0,anchor="sw")
        ctk.CTkLabel(hdr,text="⚙  Settings",font=F(18,True),text_color=T["text"]).pack(side="left",padx=24,pady=18)
        ctk.CTkLabel(hdr,text=f"stored at  {CFG_FILE}",font=FM(10),text_color=T["dim"]).pack(side="left")

        scroll=ctk.CTkScrollableFrame(sv,fg_color="transparent",
            scrollbar_button_color=T["border2"],scrollbar_button_hover_color=T["border"])
        scroll.grid(row=1,column=0,sticky="nsew",padx=20,pady=14)
        self._set_vars={}

        def section(title,accent):
            c=ctk.CTkFrame(scroll,fg_color=T["card"],corner_radius=14,border_width=1,border_color=T["border"]); c.pack(fill="x",pady=(0,14))
            h=ctk.CTkFrame(c,fg_color=T["card2"],corner_radius=0,height=38); h.pack(fill="x"); h.pack_propagate(False)
            ctk.CTkFrame(h,fg_color=accent,width=3,corner_radius=0).pack(side="left",fill="y")
            ctk.CTkLabel(h,text=f"  {title}",font=F(12,True),text_color=T["text"]).pack(side="left",padx=8)
            inner=ctk.CTkFrame(c,fg_color="transparent"); inner.pack(fill="x",padx=18,pady=14)
            return inner

        def field(parent,key,label,hint=""):
            r=ctk.CTkFrame(parent,fg_color="transparent"); r.pack(fill="x",pady=6)
            ctk.CTkLabel(r,text=label,font=F(12),text_color=T["text"],width=200,anchor="w").pack(side="left")
            var=tk.StringVar(value=str(CFG[key])); self._set_vars[key]=var
            ctk.CTkEntry(r,textvariable=var,width=120,height=32,font=FM(12),fg_color=T["raised"],
                         border_color=T["border2"],border_width=1,text_color=T["text"]).pack(side="left")
            if hint: ctk.CTkLabel(r,text="  "+hint,font=FM(9),text_color=T["dim"]).pack(side="left")

        # Appearance
        ap=section("APPEARANCE",T["cyan"])
        r=ctk.CTkFrame(ap,fg_color="transparent"); r.pack(fill="x",pady=6)
        ctk.CTkLabel(r,text="Theme",font=F(12),text_color=T["text"],width=200,anchor="w").pack(side="left")
        seg=ctk.CTkSegmentedButton(r,values=["Dark","Light"],
            command=lambda v:self._theme_from_settings(v),
            fg_color=T["raised"],selected_color=T["cyan"],selected_hover_color=T["cyanh"],
            unselected_color=T["raised"],text_color=T["text"])
        seg.set("Light" if CFG["theme"]=="light" else "Dark"); seg.pack(side="left")

        # Monitoring
        mo=section("MONITORING",T["green"])
        field(mo,"refresh_time","Refresh interval","seconds")
        field(mo,"monitor_workers","Monitor workers","parallel probes")
        field(mo,"ssh_timeout","SSH timeout","seconds")
        r=ctk.CTkFrame(mo,fg_color="transparent"); r.pack(fill="x",pady=(10,2))
        ctk.CTkLabel(r,text="X11 forwarding (SSH -X)",font=F(12),text_color=T["text"],width=200,anchor="w").pack(side="left")
        self._x11_var=tk.BooleanVar(value=bool(CFG.get("x11_forward",True)))
        ctk.CTkSwitch(r,text="",variable=self._x11_var,onvalue=True,offvalue=False,
            progress_color=T["green"],button_color=T["text"],fg_color=T["raised"]).pack(side="left")
        ctk.CTkLabel(r,text="  adds -X on “SSH in” for remote GUI apps",font=FM(9),text_color=T["dim"]).pack(side="left")
        r=ctk.CTkFrame(mo,fg_color="transparent"); r.pack(fill="x",pady=(10,2))
        ctk.CTkLabel(r,text="Job process pattern",font=F(12),text_color=T["text"],width=200,anchor="w").pack(side="left")
        var=tk.StringVar(value=CFG.get("sim_process_pattern","")); self._set_vars["sim_process_pattern"]=var
        ctk.CTkEntry(r,textvariable=var,height=32,font=FM(11),fg_color=T["raised"],
                     border_color=T["border2"],border_width=1,text_color=T["text"],
                     placeholder_text=r"(blank = count Slurm jobs only)  e.g.  my-solver|run\.sh"
                     ).pack(side="left",fill="x",expand=True)
        ctk.CTkLabel(mo,text="Regex matching your own long-running job processes, counted per node "
                             "alongside Slurm jobs. Leave blank if all your work goes through Slurm.",
                     font=FM(9),text_color=T["dim"],wraplength=520,justify="left").pack(anchor="w",pady=(4,0))

        # Transfer
        tr=section("TRANSFER  ·  throttle-safe",T["violet"])
        field(tr,"sftp_workers","SFTP workers","parallel streams")
        field(tr,"connect_retries","Connect retries","with backoff")
        field(tr,"banner_timeout","Banner timeout","seconds (raise if resets)")
        ctk.CTkLabel(tr,text="Connections are reused per worker — lower workers / higher "
                             "retries if the server resets the SSH banner under load.",
                     font=FM(9),text_color=T["dim"],wraplength=520,justify="left").pack(anchor="w",pady=(6,0))

        # Alerts
        al=section("ALERTS",T["amber"])
        field(al,"cpu_alert","CPU alert threshold","%")
        field(al,"ram_alert","RAM alert threshold","%")
        r=ctk.CTkFrame(al,fg_color="transparent"); r.pack(fill="x",pady=(10,2))
        ctk.CTkLabel(r,text="Desktop notifications",font=F(12),text_color=T["text"],width=200,anchor="w").pack(side="left")
        self._notif_var=tk.BooleanVar(value=bool(CFG.get("notifications",False)))
        ctk.CTkSwitch(r,text="",variable=self._notif_var,onvalue=True,offvalue=False,
            progress_color=T["green"],button_color=T["text"],fg_color=T["raised"]).pack(side="left")
        ctk.CTkLabel(r,text="  off = silent (no pop-up alerts)",font=FM(9),text_color=T["dim"]).pack(side="left")

        # Data source
        ds=section("DATA SOURCE",T["cyanl"])
        r=ctk.CTkFrame(ds,fg_color="transparent"); r.pack(fill="x",pady=6)
        ctk.CTkLabel(r,text="clusters.xlsx path",font=F(12),text_color=T["text"],width=200,anchor="w").pack(side="left")
        var=tk.StringVar(value=CFG["excel_path"]); self._set_vars["excel_path"]=var
        ctk.CTkEntry(r,textvariable=var,height=32,font=FM(11),fg_color=T["raised"],
                     border_color=T["border2"],border_width=1,text_color=T["text"],
                     placeholder_text="(auto-detect if blank)").pack(side="left",fill="x",expand=True,padx=(0,8))
        ctk.CTkButton(r,text="Browse",width=80,height=32,fg_color=T["raised"],hover_color=T["border2"],
            text_color=T["cyan"],font=F(11,True),command=lambda:self._pick_excel(var)).pack(side="left")

        # actions
        bar=ctk.CTkFrame(sv,fg_color=T["panel"],corner_radius=0,height=58); bar.grid(row=2,column=0,sticky="ew"); bar.grid_propagate(False)
        ctk.CTkFrame(bar,fg_color=T["border"],height=1,corner_radius=0).place(x=0,y=0,relwidth=1.0)
        self._set_msg=ctk.CTkLabel(bar,text="",font=FM(10),text_color=T["green"]); self._set_msg.pack(side="left",padx=20)
        ctk.CTkButton(bar,text="Reset defaults",width=130,height=36,fg_color=T["raised"],hover_color=T["border2"],
            text_color=T["muted"],font=F(12),corner_radius=8,command=self._reset_settings).pack(side="right",padx=(0,14),pady=11)
        ctk.CTkButton(bar,text="✓  Save & Apply",width=150,height=36,fg_color=T["green"],hover_color=T["greenh"],
            text_color=T["void"],font=F(13,True),corner_radius=8,command=self._save_settings).pack(side="right",padx=(0,6),pady=11)

    def _theme_from_settings(self,v):
        name="light" if v=="Light" else "dark"
        if name!=CFG["theme"]:
            apply_theme(name); save_config(); self._build()

    def _pick_excel(self,var):
        p=filedialog.askopenfilename(title="Select clusters.xlsx",
                                     filetypes=[("Excel","*.xlsx *.xlsm"),("All","*.*")])
        if p: var.set(p)

    def _reset_settings(self):
        for k,v in DEFAULTS.items():
            if k in self._set_vars: self._set_vars[k].set(str(v))
        if hasattr(self,"_notif_var"): self._notif_var.set(bool(DEFAULTS["notifications"]))
        if hasattr(self,"_x11_var"): self._x11_var.set(bool(DEFAULTS["x11_forward"]))
        if self._set_msg: self._set_msg.configure(text="Defaults loaded — Save to apply.")

    def _save_settings(self):
        ints={"refresh_time","monitor_workers","sftp_workers","connect_retries","banner_timeout","ssh_timeout"}
        flts={"cpu_alert","ram_alert"}
        try:
            for k,var in self._set_vars.items():
                val=var.get().strip()
                if k in ints:   CFG[k]=max(1,int(float(val)))
                elif k in flts: CFG[k]=max(0.0,min(100.0,float(val)))
                else:           CFG[k]=val
        except ValueError:
            messagebox.showwarning("Invalid value","Numeric fields must be numbers."); return
        if hasattr(self,"_notif_var"):
            CFG["notifications"]=bool(self._notif_var.get())
        if hasattr(self,"_x11_var"):
            CFG["x11_forward"]=bool(self._x11_var.get())
        save_config()
        # apply live
        try: self._pool.shutdown(wait=False)
        except: pass
        self._pool=ThreadPoolExecutor(max_workers=CFG["monitor_workers"])
        self._clusters=load_clusters_xlsx()
        if self._set_msg: self._set_msg.configure(text=f"Saved ✓  ({time.strftime('%H:%M:%S')})")
        self._update_statusbar()
        self._build()                       # rebuild so new clusters / labels show
        self._show_view("settings")

    # ── reload clusters ──
    def _reload_clusters(self):
        self._clusters=load_clusters_xlsx()
        register_clusters(self._clusters)
        reset_gateways()
        self._build()
        self.after(300,self._refresh_cycle)


# ═══════════════════════════════════════════════════════════════════════════════
#   ENTRY POINT
#     clusteros            launch the GUI
#     clusteros --report   headless one-shot status report on stdout
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    if "--report" in sys.argv:
        clusters=load_clusters_xlsx()
        if not clusters:
            sys.exit("No clusters found. Copy clusters.example.xlsx to "
                     "clusters.xlsx and add your nodes.")
        register_clusters(clusters)
        lines=["═"*56,"  CLUSTER REPORT","═"*56]
        def probe_one(r):
            online=is_online(r); st=get_remote_status(r) if online else None
            return r,online,st
        with ThreadPoolExecutor(max_workers=max(2,len(clusters) or 2)) as pool:
            results=list(pool.map(probe_one,clusters))
        for r,online,st in results:
            if not online: lines.append(f"  ✗  {r['hostname']:<16} OFFLINE")
            elif st is None: lines.append(f"  ⚠  {r['hostname']:<16} SSH ERROR")
            else:
                lines.append(f"  ●  {r['hostname']:<16} CPU {float(st['cpu']):.0f}%  "
                             f"RAM {float(st['ram']):.0f}%  GPU {st['gpu']}%  up {fmt_uptime(st['uptime'])}")
        lines.append("═"*56); print("\n".join(lines))
    else:
        ClusterOS().mainloop()


if __name__ == "__main__":
    main()
