#!/bin/bash
# Local Time Tracker — one-line installer for macOS
#   curl -fsSL https://raw.githubusercontent.com/areeb1501/local-time-tracker-macos/main/install.sh | bash
#
# Options (env):  TT_APP_DIR  install location   (default ~/.local/share/local-time-tracker)
#                 TT_YES=1    skip onboarding prompts (accept defaults)
#                 TT_NO_START=1  install only, don't start tracking
#                 TT_SOURCE   install from a local checkout instead of downloading
set -euo pipefail

REPO="${TT_REPO:-areeb1501/local-time-tracker-macos}"
APP="${TT_APP_DIR:-$HOME/.local/share/local-time-tracker}"
BIN="$HOME/.local/bin"

b=$'\e[1m'; d=$'\e[2m'; g=$'\e[32m'; r=$'\e[31m'; c=$'\e[36m'; x=$'\e[0m'
say()  { printf "%s\n" "$*"; }
ok()   { say "  ${g}✓${x} $*"; }
die()  { say "  ${r}✗${x} $*" >&2; exit 1; }

say ""
say "${b}  ⏱  Local Time Tracker${x} ${d}— installer${x}"
say ""

[[ "$(uname -s)" == "Darwin" ]] || die "macOS only (it reads the frontmost window via macOS APIs)."
ok "macOS $(sw_vers -productVersion)"

# Python 3.10+ (macOS's /usr/bin/python3 is often 3.9)
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
if [[ -z "$PY" ]]; then
  if command -v brew >/dev/null 2>&1; then
    say "  Python 3.10+ not found — installing python@3.12 with Homebrew…"
    brew install python@3.12 >/dev/null && PY="$(brew --prefix)/bin/python3.12"
  else
    die "Python 3.10+ is required. Install it from https://www.python.org/downloads/ or with Homebrew, then re-run."
  fi
fi
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')  ${d}($PY)${x}"

# Fetch the code
mkdir -p "$APP"
if [[ -n "${TT_SOURCE:-}" ]]; then
  rsync -a --exclude .venv --exclude .git --exclude docs --exclude scripts "$TT_SOURCE/" "$APP/"
  ok "copied from $TT_SOURCE"
else
  TMP="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" | tar -xz -C "$TMP" --strip-components 1 \
    || die "download failed"
  rsync -a --exclude .venv --exclude docs --exclude scripts "$TMP/" "$APP/"
  rm -rf "$TMP"
  ok "downloaded $REPO → ${d}$APP${x}"
fi

# Isolated virtualenv (nothing is installed globally)
[[ -x "$APP/.venv/bin/python" ]] || "$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null
"$APP/.venv/bin/python" -m pip install -q -r "$APP/requirements.txt" || die "dependency install failed"
ok "dependencies installed ${d}(FastAPI, uvicorn, pyobjc — in a private venv)${x}"

chmod +x "$APP/bin/tt"
mkdir -p "$BIN"
ln -sf "$APP/bin/tt" "$BIN/tt"
ok "command ${b}tt${x} → $BIN/tt"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) RC="$HOME/.zshrc"; [[ "${SHELL:-}" == */bash ]] && RC="$HOME/.bash_profile"
     grep -qs 'local/bin' "$RC" || printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
     say "  ${d}added ~/.local/bin to PATH in $RC — open a new terminal (or: export PATH=\"\$HOME/.local/bin:\$PATH\")${x}";;
esac

# Onboarding (reads answers from the terminal even when piped through curl)
ARGS=()
[[ -n "${TT_YES:-}" ]] && ARGS+=(--yes)
[[ -n "${TT_NO_START:-}" ]] && ARGS+=(--no-start)
if [[ -z "${TT_YES:-}" ]] && ! { : </dev/tty; } 2>/dev/null; then ARGS+=(--yes); fi
"$APP/bin/tt" setup "${ARGS[@]+"${ARGS[@]}"}"
