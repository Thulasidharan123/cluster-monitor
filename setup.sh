#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  ClusterOS installer
#
#  Installs into the Python environment you are ALREADY IN. It does not create
#  or activate anything — activate your venv / conda env first, then run this.
#
#    ./setup.sh              install into the active environment
#    ./setup.sh --check      only report on prerequisites, install nothing
#    ./setup.sh --user       install into the per-user site-packages (pip --user)
#    ./setup.sh --venv       opt in to creating a local .venv (old behaviour)
#    ./setup.sh --python X   use interpreter X instead of the active one
#
#  Safe to re-run.
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── output helpers ────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; CYN=""; RST=""
fi
WARNINGS=0; ERRORS=0
ok()   { printf '  %s✓%s %s\n'  "$GRN" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n'  "$YLW" "$RST" "$*"; WARNINGS=$((WARNINGS+1)); }
bad()  { printf '  %s✗%s %s\n'  "$RED" "$RST" "$*"; ERRORS=$((ERRORS+1)); }
info() { printf '    %s%s%s\n'  "$DIM" "$*" "$RST"; }
head1(){ printf '\n%s%s%s\n' "$BOLD$CYN" "$*" "$RST"; }

# ── options ───────────────────────────────────────────────────────────────────
MODE="current"          # current | check | user | venv
PY_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check)  MODE="check" ;;
    --user)   MODE="user"  ;;
    --venv)   MODE="venv"  ;;
    --python) shift; PY_OVERRIDE="${1:-}" ;;
    --python=*) PY_OVERRIDE="${1#*=}" ;;
    -h|--help) sed -n '3,14p' "$0" | sed 's/^# \{0,2\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

printf '%s\n' "${BOLD}ClusterOS setup${RST}"

case "$(uname -s)" in
  Darwin) OS="macos"  ;;
  Linux)  OS="linux"  ;;
  MINGW*|MSYS*|CYGWIN*) OS="windows" ;;
  *)      OS="unknown" ;;
esac
info "platform: $OS"

# ── pick the interpreter: the ACTIVE one, unless told otherwise ───────────────
head1 "Python environment"

pick_python() {
  # explicit override wins
  if [ -n "$PY_OVERRIDE" ]; then
    command -v "$PY_OVERRIDE" >/dev/null 2>&1 || { bad "no such interpreter: $PY_OVERRIDE"; exit 1; }
    echo "$PY_OVERRIDE"; return
  fi
  # an activated venv / conda env exports these — use its interpreter directly
  if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    echo "$VIRTUAL_ENV/bin/python"; return
  fi
  if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
    echo "$CONDA_PREFIX/bin/python"; return
  fi
  # otherwise whatever `python3` / `python` resolves to on PATH
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 && { echo "$c"; return; }
  done
  echo ""
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  bad "No Python found on PATH."
  case "$OS" in
    macos) info "install with:  brew install python@3.12" ;;
    linux) info "install with:  sudo apt install python3 python3-pip" ;;
  esac
  exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
  bad "Python 3.9+ required — active interpreter is $("$PY" -V 2>&1)"
  info "activate a newer environment, or:  ./setup.sh --python python3.12"
  exit 1
fi

# describe what we found, so nobody installs into the wrong place by accident
ENV_KIND="system Python"
if   [ -n "${VIRTUAL_ENV:-}" ]; then ENV_KIND="active venv  ($VIRTUAL_ENV)"
elif [ -n "${CONDA_PREFIX:-}" ]; then ENV_KIND="active conda env  (${CONDA_DEFAULT_ENV:-$CONDA_PREFIX})"
elif "$PY" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null; then
  ENV_KIND="virtual environment"
fi

ok "$("$PY" -V 2>&1) — $("$PY" -c 'import sys; print(sys.executable)')"
ok "target: $ENV_KIND"

if [ "$ENV_KIND" = "system Python" ] && [ "$MODE" = "current" ]; then
  warn "installing into your SYSTEM Python."
  info "that is fine, but if it is externally managed pip may refuse — in that"
  info "case re-run with  --user  (per-user install) or  --venv  (local .venv)."
fi

# ── tkinter ───────────────────────────────────────────────────────────────────
if "$PY" -c 'import tkinter' >/dev/null 2>&1; then
  ok "tkinter available"
else
  bad "tkinter is missing from this environment — ClusterOS is a Tk app and"
  info "cannot start without it. It is NOT installable with pip."
  case "$OS" in
    macos) info "Homebrew python:  brew install python-tk"
           info "or install a python.org build, which bundles it" ;;
    linux) info "Debian/Ubuntu:  sudo apt install python3-tk"
           info "Fedora/RHEL:    sudo dnf install python3-tkinter"
           info "Arch:           sudo pacman -S tk" ;;
  esac
  [ -n "${CONDA_PREFIX:-}" ] && info "conda env:      conda install tk"
fi

# ── system prerequisites ──────────────────────────────────────────────────────
head1 "System prerequisites"

if command -v ssh >/dev/null 2>&1; then
  ok "ssh — $(command -v ssh)"
else
  bad "ssh not found. Install an OpenSSH client."
fi

# sshpass: only for password auth in interactive terminals. Key auth works without it.
if command -v sshpass >/dev/null 2>&1; then
  ok "sshpass — $(command -v sshpass)"
else
  warn "sshpass not found — needed only to open interactive terminals to nodes"
  info "using PASSWORD auth. Monitoring and transfers work without it."
  case "$OS" in
    macos) info "install with:  brew install hudochenkov/sshpass/sshpass" ;;
    linux) info "Debian/Ubuntu:  sudo apt install sshpass"
           info "Fedora/RHEL:    sudo dnf install sshpass" ;;
  esac
  info "or skip it by using SSH keys (recommended):"
  info "  ssh-keygen -t ed25519 && ssh-copy-id user@node"
fi

# X server: only for X11 forwarding of remote GUI apps
case "$OS" in
  macos)
    if [ -d /Applications/Utilities/XQuartz.app ] || [ -d /opt/X11 ]; then
      ok "XQuartz present (X11 forwarding available)"
    else
      warn "XQuartz not found — optional. Needed only to display remote GUI apps"
      info "locally via 'ssh -X'. Install:  brew install --cask xquartz"
      info "or turn x11_forward off in Settings."
    fi ;;
  linux)
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
      ok "display server detected"
    else
      warn "no DISPLAY set — ClusterOS needs a graphical session for its window."
      info "'clusteros --report' still works headless."
    fi ;;
esac

if [ "$MODE" = "check" ]; then
  head1 "Summary"
  printf '  %d error(s), %d warning(s)\n\n' "$ERRORS" "$WARNINGS"
  [ "$ERRORS" -eq 0 ]
  exit $?
fi

# ── install ───────────────────────────────────────────────────────────────────
head1 "Installing dependencies"

PIP_ARGS=()
case "$MODE" in
  venv)
    if [ -d .venv ]; then
      ok "reusing existing .venv"
    else
      "$PY" -m venv .venv || {
        bad "venv creation failed. Debian/Ubuntu:  sudo apt install python3-venv"; exit 1; }
      ok "created .venv"
    fi
    PY=".venv/bin/python"; [ -x "$PY" ] || PY=".venv/Scripts/python.exe"
    ;;
  user)
    PIP_ARGS+=(--user)
    ok "installing into the per-user site-packages"
    ;;
  current)
    ok "installing into the environment above — no new environment created"
    ;;
esac

"$PY" -m pip --version >/dev/null 2>&1 || {
  bad "pip is not available in this environment."
  info "try:  $PY -m ensurepip --upgrade"
  exit 1
}

"$PY" -m pip install --upgrade pip "${PIP_ARGS[@]}" --quiet 2>/dev/null || true

INSTALL_OUT=""
if INSTALL_OUT="$("$PY" -m pip install -e ".[display]" "${PIP_ARGS[@]}" 2>&1)"; then
  ok "installed clusteros + dependencies"
else
  # PEP 668: externally-managed system Python refuses to install
  if printf '%s' "$INSTALL_OUT" | grep -q "externally-managed-environment"; then
    bad "this Python is externally managed, so pip refuses to install into it."
    info "pick one:"
    info "  ./setup.sh --user     install just for your user (simplest)"
    info "  ./setup.sh --venv     create a local .venv here"
    info "  activate your own venv/conda env, then re-run ./setup.sh"
  else
    bad "pip install failed:"
    printf '%s\n' "$INSTALL_OUT" | tail -20 | sed 's/^/      /'
  fi
  exit 1
fi

# where did the launcher land?
if [ "$MODE" = "venv" ]; then
  ok "launcher: $ROOT/.venv/bin/clusteros  (after activating .venv)"
elif LAUNCHER="$(command -v clusteros 2>/dev/null)" && [ -n "$LAUNCHER" ]; then
  ok "launcher on PATH: $LAUNCHER"
elif [ "$MODE" = "user" ]; then
  USERBASE="$("$PY" -c 'import site; print(site.USER_BASE)' 2>/dev/null || echo "$HOME/.local")"
  warn "'clusteros' is not on your PATH yet."
  info "add this to your shell profile:"
  info "  export PATH=\"$USERBASE/bin:\$PATH\""
  info "or just run:  $PY clusteros.py"
else
  warn "'clusteros' is not on your PATH — run it as:  $PY clusteros.py"
fi

# ── configuration scaffolding ─────────────────────────────────────────────────
head1 "Configuration"
if [ -f clusters.xlsx ]; then
  ok "clusters.xlsx exists — left untouched"
else
  cp clusters.example.xlsx clusters.xlsx
  ok "created clusters.xlsx from the example template"
  info "EDIT IT: replace the sample rows with your own nodes."
  info "It is gitignored — it holds credentials and must never be committed."
fi

mkdir -p "$HOME/.clusteros"
if [ -f "$HOME/.clusteros/config.json" ]; then
  ok "~/.clusteros/config.json exists — left untouched"
else
  info "no ~/.clusteros/config.json yet — defaults apply."
  info "See config.example.json, or just use the Settings tab in the app."
fi

# ── done ──────────────────────────────────────────────────────────────────────
head1 "Done"
printf '  %d warning(s)\n\n' "$WARNINGS"
echo "  Next:"
echo "    1. Edit clusters.xlsx with your nodes (see its README sheet)."
if [ "$MODE" = "venv" ]; then
  echo "    2. source .venv/bin/activate"
  echo "    3. clusteros"
else
  echo "    2. clusteros            ${DIM}(or: $PY clusteros.py)${RST}"
fi
echo ""
echo "  Headless status report:  clusteros --report"
echo ""
