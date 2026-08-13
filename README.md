# ClusterOS

A single desktop console for a fleet of SSH-reachable machines. Watch live CPU,
RAM, GPU, disk and Slurm queues across every node; move files between any two of
them; open terminals, browse remote filesystems, and take over a remote screen —
all from one window, without juggling a dozen terminal tabs.

Built with Tkinter + CustomTkinter and Paramiko. Runs on macOS, Linux and
Windows.

---

## Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Node inventory — `clusters.xlsx`](#node-inventory--clustersxlsx)
  - [Application settings — `config.json`](#application-settings--configjson)
  - [Slurm overrides](#slurm-overrides)
- [Usage](#usage)
- [Gateway / jump-host routing](#gateway--jump-host-routing)
- [Remote Desktop](#remote-desktop)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Features

**Monitor** — a responsive tile grid, one card per node, reflowing to the window
width. Each card shows live CPU / RAM / GPU / disk with sparkline history,
uptime, load, running simulation count, and online state. Nodes running Slurm are
auto-detected and grow a queue panel: your pending and running jobs, cores in
use, and the per-user core cap.

**Transfer** — two endpoint panels, either of which can be your local machine or
any node. Drag a path in, pick a destination, go. Transfers stream over pooled,
reused SSH connections with retry-and-backoff, which is what keeps busy SSH
servers from dropping you with *"Error reading SSH protocol banner"*. Directory
trees, progress per file, and parallel streams are all handled.

**Remote file browser** — an SFTP file picker on any node, so you can point at a
path without guessing it.

**Terminals** — one click opens an interactive SSH session in your system
terminal, optionally with X11 forwarding so remote GUI apps draw on your desktop.

**Remote Desktop** — screen-share into a node: ClusterOS launches a capture agent
over SSH and opens a local viewer window with mouse and keyboard passthrough.

**Jump-host routing** — nodes on an internal LAN that you cannot reach directly
are tunneled through one that you can, transparently. See
[below](#gateway--jump-host-routing).

**Themes and settings** — live light/dark toggle, and a Settings tab for poll
interval, worker counts, retry behaviour, alert thresholds and inventory path.

---

## Prerequisites

### Required

| What | Why | Install |
|---|---|---|
| **Python 3.9+** | Runtime | [python.org](https://www.python.org/downloads/), `brew install python@3.12`, or your distro's package |
| **tkinter** | ClusterOS is a Tk app and will not start without it | Bundled with most CPython builds. Debian/Ubuntu: `sudo apt install python3-tk` · Fedora: `sudo dnf install python3-tkinter` · Arch: `sudo pacman -S tk` · macOS Homebrew: `brew install python-tk` |
| **OpenSSH client** | All connectivity | Preinstalled on macOS/Linux. Windows: *Settings → Apps → Optional features → OpenSSH Client* |
| **A graphical session** | It's a GUI | Any desktop environment. (`clusteros --report` runs headless.) |

### Optional

| What | Needed for | Install |
|---|---|---|
| **sshpass** | Opening *interactive terminals* to nodes that use **password** auth. Monitoring and transfers do not need it. | macOS: `brew install hudochenkov/sshpass/sshpass` · Debian/Ubuntu: `sudo apt install sshpass` · Fedora: `sudo dnf install sshpass` |
| **XQuartz** (macOS) | X11 forwarding, so remote GUI apps display locally | `brew install --cask xquartz` |
| **screeninfo** | Placing the window on an extended display | `pip install "clusteros[display]"` |
| **pygame, opencv-python, numpy** | Remote Desktop — the local viewer half | `pip install "clusteros[remote-desktop]"` |
| **mss, opencv-python, numpy, pynput** | Remote Desktop — installed **on the remote node**, not locally | `pip install mss opencv-python numpy pynput` |

> **Skip sshpass entirely by using SSH keys**, which is the better setup anyway:
> ```bash
> ssh-keygen -t ed25519
> ssh-copy-id alice@10.0.0.10
> ```
> Then put the key path in the `key` column of `clusters.xlsx` and leave
> `password` blank.

### Remote nodes

Each monitored node needs nothing more than an SSH server and a POSIX shell.
The probe uses standard tools and degrades gracefully when one is missing:

- `top`, `free`, `df`, `nproc`, `uptime` — core metrics
- `nvidia-smi` — GPU stats (skipped if absent)
- `sinfo`, `squeue` — Slurm panel (skipped if absent)

---

## Installation

### Quick — the setup script

`setup.sh` installs into **the Python environment you are already in**. It does
not create or activate anything — activate your venv or conda env first (or
don't, to use your system Python), then run it.

```bash
git clone https://github.com/YOUR_USERNAME/clusteros.git
cd clusteros

# already inside the env you want? just:
./setup.sh
```

It verifies your Python and system prerequisites, tells you exactly what to
install if something is missing, installs ClusterOS and its dependencies into
the active environment, and seeds `clusters.xlsx` from the example template.
It is safe to re-run.

| Flag | Effect |
|---|---|
| *(none)* | Install into the active environment — venv, conda env, or system Python |
| `--check` | Report on prerequisites only, install nothing |
| `--user` | Install into your per-user site-packages (`pip --user`) |
| `--venv` | Opt in to creating a local `.venv` here |
| `--python X` | Use interpreter `X` instead of the active one |

The script prints which environment it is about to install into before it does
anything, so you can Ctrl-C if it picked the wrong one:

```
Python environment
  ✓ Python 3.12.3 — /home/you/miniconda3/envs/work/bin/python
  ✓ target: active conda env  (work)
```

Then:

```bash
clusteros
```

### Manual

```bash
git clone https://github.com/YOUR_USERNAME/clusteros.git
cd clusteros

# activate your environment first, if you use one
pip install -e .                   # or: pip install -r requirements.txt
cp clusters.example.xlsx clusters.xlsx

clusteros                          # or: python clusteros.py
```

### If pip refuses: "externally-managed-environment"

Recent Debian, Ubuntu, Fedora and Homebrew Pythons block installs into the
system site-packages (PEP 668). Pick whichever suits you:

```bash
./setup.sh --user     # per-user install, no root, no new environment
./setup.sh --venv     # create a local .venv here
# — or activate your own venv/conda env and re-run ./setup.sh
```

With `--user`, make sure the user bin directory is on your `PATH`:

```bash
export PATH="$(python3 -c 'import site; print(site.USER_BASE)')/bin:$PATH"
```

### Windows

`setup.sh` needs a bash shell (Git Bash or WSL). Otherwise activate your
environment and run the manual steps in PowerShell. Note that `sshpass` is not
available on Windows — use SSH keys.

---

## Configuration

Two files, with a clear split: **`clusters.xlsx` holds credentials**,
**`config.json` holds preferences**. Only the second is ever safe to share.

### Node inventory — `clusters.xlsx`

Copy `clusters.example.xlsx` to `clusters.xlsx` and replace the sample rows.
The workbook's `README` sheet documents every column; the header cells carry
the same notes as comments.

| Column | Required | Meaning |
|---|---|---|
| `hostname` | ✅ | Display name. Other rows reference it in `via`. Aliases: `name` |
| `ip` | ✅ | IP address or DNS name. Aliases: `address` |
| `username` | ✅ | SSH username. Aliases: `user` |
| `password` | — | SSH password. Leave blank when using a key. Aliases: `pass` |
| `key` | — | Path to a private key file. Wins over `password`. Aliases: `keyfile` |
| `via` | — | `hostname` of another row to tunnel through. Aliases: `jump`, `gateway` |
| `host_dir` | — | Remote folder holding `host.py`, for Remote Desktop. Default `~/.pydesk`. Aliases: `host_folder`, `host_path`, `desktop_dir`, `rd_dir` |

Headers are matched case-insensitively and column order does not matter. Rows
are read from the first sheet.

ClusterOS looks for the workbook in this order: the `excel_path` setting, then
next to `clusteros.py`, then the current directory, then your home directory.

> ⚠️ **`clusters.xlsx` is in `.gitignore` and must stay there.** It contains
> hostnames, usernames and passwords for your infrastructure. Only
> `clusters.example.xlsx`, with its dummy rows, belongs in the repository.

### Application settings — `config.json`

Lives at `~/.clusteros/config.json`. The Settings tab writes it for you, so you
rarely need to touch it by hand. Every key is optional. See
`config.example.json` for the annotated version.

| Key | Default | What it does |
|---|---|---|
| `theme` | `"dark"` | `"dark"` or `"light"` |
| `refresh_time` | `10` | Seconds between monitor polls |
| `monitor_workers` | `8` | Parallel status probes |
| `sftp_workers` | `3` | Parallel SFTP streams; also caps concurrent handshakes |
| `connect_retries` | `4` | SSH attempts before a node is marked offline |
| `banner_timeout` | `30` | Seconds to wait for the SSH banner |
| `ssh_timeout` | `5` | Base connect/command timeout |
| `cpu_alert` | `90.0` | CPU % that turns a tile red |
| `ram_alert` | `90.0` | RAM % that turns a tile red |
| `excel_path` | `""` | Absolute path to the inventory workbook; blank = search |
| `notifications` | `false` | Desktop pop-ups on threshold breach (macOS/Linux) |
| `gateway` | `""` | Node to tunnel through; blank = auto-detect |
| `auto_gateway` | `true` | Auto-pick a jump host for unreachable nodes |
| `sim_process_pattern` | `""` | Regex matching your own long-running job processes; blank = Slurm jobs only |
| `x11_forward` | `true` | Add `ssh -X` when opening terminals |

**Counting non-Slurm jobs.** Each node tile shows how many jobs you have
running. Slurm jobs are counted automatically. If you also launch work outside
the scheduler, set `sim_process_pattern` to an extended regex matching those
processes and they'll be counted too:

```json
"sim_process_pattern": "my-solver|run\\.sh|mpirun"
```

It's blank by default, which counts Slurm jobs only. You can also set it from
the Settings tab.

**Tuning against a throttling SSH server:** lower `monitor_workers` and
`sftp_workers`, raise `banner_timeout` and `connect_retries`, and lengthen
`refresh_time`. `MaxStartups` on the remote `sshd` is usually what's biting you.

### Slurm overrides

Slurm is auto-detected per node at probe time, so a Slurm cluster lights up its
queue panel with no configuration at all. Edit the `SLURM_HOSTS` dict near the
top of `clusteros.py` only to pin something the probe cannot infer:

```python
SLURM_HOSTS = {
    "my-cluster": {
        "partition":  "gpu",   # restrict to one partition; "" = all
        "user":       "alice", # whose jobs to show; default = the SSH username
        "core_limit": 64,      # per-user CPU-CORE cap; 0 = auto-detect
    },
}
```

`core_limit` is a **core** count, not a node count.

---

## Usage

```bash
clusteros              # launch the GUI
clusteros --report     # headless status report on stdout, then exit
python clusteros.py    # equivalent, without installing
```

The `--report` mode probes every node in parallel and prints a one-line summary
each — useful from cron, a login shell, or a status endpoint.

The left rail switches between **Monitor**, **Transfer** and **Settings**, and
carries the theme toggle and a reload button that re-reads `clusters.xlsx` and
re-decides all routes.

---

## Gateway / jump-host routing

In most real deployments only a couple of nodes are reachable from where you
sit; the rest live on an internal LAN that one of those reachable nodes can see.
ClusterOS tunnels SSH and SFTP to the unreachable nodes **through** a reachable
one, over an SSH `direct-tcpip` channel — exactly `ssh -J` / `ProxyJump`, done
in-process.

This is a TCP-level relay, **not** a two-hop copy. Bytes pass through the
gateway's network stack and land directly on the target's disk. Nothing is ever
staged on the gateway, so there is no double copy and no disk-space requirement
there.

The jump host is chosen in this order:

1. the per-node `via` column in `clusters.xlsx`
2. the global `gateway` setting
3. automatic — if `auto_gateway` is on, the node whose IP shares the longest
   prefix with the target, preferring one that is reachable right now

Routes are cached and re-decided whenever reachability changes or you hit
Reload.

---

## Remote Desktop

Pressing **Desktop** on a node card:

1. SSHes in (reusing the pooled connection and any gateway route),
2. detects the live X11 display and Xauthority cookie, and launches `host.py`
   detached in the node's `host_dir` (default `~/.pydesk`),
3. waits for the stream port to open, then runs `viewer.py` locally.

`host.py` and `protocol.py` must exist in the remote `host_dir`; if they are
also present locally, ClusterOS refreshes them over SFTP so the versions match.

Dependencies split across the two machines:

```bash
# on the remote node
pip install mss opencv-python numpy pynput

# on your machine
pip install "clusteros[remote-desktop]"
```

Both halves can also be driven by hand:

```bash
# remote
python host.py --password mysecret --fps 15 --quality 60

# local
python viewer.py --host 10.0.0.11 --password mysecret
```

---

## Security notes

Read this before pushing anything anywhere.

- **`clusters.xlsx` holds plaintext credentials.** It is gitignored. Keep it
  that way. Prefer SSH keys — fill the `key` column and leave `password` empty.
- `chmod 600 clusters.xlsx` so only you can read it.
- ClusterOS uses Paramiko's `AutoAddPolicy`, which accepts unknown host keys
  without prompting. That is convenient on a trusted network and weak against
  an active MITM. If that matters to you, swap it for `RejectPolicy` and
  pre-populate `~/.ssh/known_hosts`.
- If you ever committed a real `clusters.xlsx` or a password, rotate the
  credentials — removing the file in a later commit does not remove it from
  history.
- The Remote Desktop stream is protected by the password you pass it, over a
  plain TCP socket. Run it across a trusted network or tunnel it.

---

## Troubleshooting

**`Missing package — run: pip install ...`**
Dependencies aren't installed, or you're in a different environment than the one
you installed into. Activate the right environment and run `./setup.sh` again —
it reports which interpreter it targets before installing.

**`error: externally-managed-environment`**
Your Python blocks system-wide installs. Run `./setup.sh --user`, or
`./setup.sh --venv`, or activate your own environment first — see
[Installation](#if-pip-refuses-externally-managed-environment).

**`clusteros: command not found` after a successful install**
The launcher landed in a bin directory that isn't on your `PATH` — common with
`--user`. Either add it (the script prints the exact `export` line) or just run
`python clusteros.py` from the repo.

**`ModuleNotFoundError: No module named 'tkinter'`**
tkinter is a separate OS package on many distros — see
[Prerequisites](#prerequisites).

**Nodes show OFFLINE but `ssh` works from a terminal**
Check that `ip`, `username` and `password`/`key` in the workbook are right and
that the key path is readable. If the node is on an internal LAN, set `via` to a
reachable node, or turn `auto_gateway` on.

**"Error reading SSH protocol banner — Connection reset by peer"**
The remote `sshd` is throttling concurrent connections. Lower
`monitor_workers` and `sftp_workers`, raise `banner_timeout`, and lengthen
`refresh_time`.

**Terminal won't open to a password-auth node**
Install `sshpass`, or switch that node to key auth.

**Remote GUI apps don't display**
Install XQuartz (macOS) or an X server, confirm `x11_forward` is on, and check
that `X11Forwarding yes` is set in the remote `sshd_config`.

**No Slurm panel on a Slurm cluster**
The probe looks for `sinfo` and `squeue` on the node's `PATH` for a
non-interactive shell. Make sure the Slurm module is loaded in `~/.bashrc` in a
way that applies to non-login shells.

**Nothing appears in the monitor at all**
`clusters.xlsx` wasn't found or has no data rows. Run `clusteros --report` — it
tells you plainly if the inventory is empty.

---

## License

MIT — see [LICENSE](LICENSE).
