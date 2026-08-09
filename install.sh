#!/usr/bin/env bash
set -euo pipefail

TARGET="agents"
INSTALL_DEPS=1
DESTINATION=""

usage() {
  echo "Usage: ./install.sh [--target agents|codex|claude|copilot|openclaw|hermes|all] [--destination PATH] [--no-deps]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --destination) DESTINATION="${2:-}"; shift 2 ;;
    --no-deps) INSTALL_DEPS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

case "$TARGET" in
  agents|codex|claude|copilot|openclaw|hermes|all) ;;
  *) echo "Invalid target: $TARGET" >&2; exit 2 ;;
esac
if [ -n "$DESTINATION" ] && [ "$TARGET" = "all" ]; then
  echo "--destination cannot be combined with --target all" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/skills/wechat-article-subscriber"
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "Python 3.9+ is required" >&2
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Python 3.9+ is required" >&2
  exit 1
}

target_parent() {
  local profile_root="${WECHAT_SKILL_INSTALL_ROOT:-$HOME}"
  case "$1" in
    agents) printf '%s\n' "$profile_root/.agents/skills" ;;
    codex)
      if [ -n "${WECHAT_SKILL_INSTALL_ROOT:-}" ]; then
        printf '%s\n' "$profile_root/.codex/skills"
      else
        printf '%s\n' "${CODEX_HOME:-$HOME/.codex}/skills"
      fi ;;
    claude) printf '%s\n' "$profile_root/.claude/skills" ;;
    copilot) printf '%s\n' "$profile_root/.copilot/skills" ;;
    openclaw) printf '%s\n' "$profile_root/.openclaw/skills" ;;
    hermes) printf '%s\n' "$profile_root/.hermes/skills" ;;
  esac
}

data_home() {
  if [ -n "${WECHAT_ARTICLE_HOME:-}" ]; then
    "$PYTHON_BIN" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$WECHAT_ARTICLE_HOME"
  elif [ "$(uname -s)" = "Darwin" ]; then
    printf '%s\n' "$HOME/Library/Application Support/wechat-article-subscriber"
  else
    printf '%s\n' "${XDG_STATE_HOME:-$HOME/.local/state}/wechat-article-subscriber"
  fi
}

backup_path() {
  local destination="$1" timestamp candidate counter
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  candidate="$destination.backup.$timestamp"
  counter=1
  while [ -e "$candidate" ]; do
    candidate="$destination.backup.$timestamp.$counter"
    counter=$((counter + 1))
  done
  printf '%s\n' "$candidate"
}

declare -a KINDS=() DESTINATIONS=() TEMPORARIES=() BACKUPS=() HAD_EXISTING=() COMMITTED=()
prepare_skill() {
  local kind="$1" parent destination temporary backup index
  if [ -n "$DESTINATION" ]; then
    destination="$("$PYTHON_BIN" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$DESTINATION")"
    parent="$(dirname "$destination")"
  else
    parent="$(target_parent "$kind")"
    destination="$parent/wechat-article-subscriber"
  fi
  mkdir -p "$parent"
  temporary="$(mktemp -d "$parent/.wechat-article-subscriber.install.XXXXXX")"
  cp "$SOURCE_DIR/SKILL.md" "$SOURCE_DIR/requirements.txt" "$temporary/"
  mkdir "$temporary/agents" "$temporary/scripts" "$temporary/references"
  cp "$SOURCE_DIR/agents/"*.yaml "$temporary/agents/"
  cp "$SOURCE_DIR/scripts/"*.py "$SOURCE_DIR/scripts/"*.sh "$SOURCE_DIR/scripts/"*.ps1 "$temporary/scripts/"
  cp "$SOURCE_DIR/references/"*.md "$temporary/references/"
  if [ -d "$SOURCE_DIR/assets" ]; then cp -R "$SOURCE_DIR/assets" "$temporary/"; fi
  backup="$(backup_path "$destination")"
  index="${#KINDS[@]}"
  KINDS[index]="$kind"
  DESTINATIONS[index]="$destination"
  TEMPORARIES[index]="$temporary"
  BACKUPS[index]="$backup"
  if [ -e "$destination" ]; then HAD_EXISTING[index]=1; else HAD_EXISTING[index]=0; fi
  COMMITTED[index]=0
}

rollback() {
  local index destination temporary backup
  if [ "$VENV_COMMITTED" -eq 1 ] && [ -e "$VENV_DIR" ]; then rm -rf -- "$VENV_DIR"; fi
  if [ "$VENV_HAD_EXISTING" -eq 1 ] && [ -e "$VENV_BACKUP" ] && [ ! -e "$VENV_DIR" ]; then
    mv "$VENV_BACKUP" "$VENV_DIR"
  fi
  for ((index=${#KINDS[@]}-1; index>=0; index--)); do
    destination="${DESTINATIONS[index]}"
    temporary="${TEMPORARIES[index]}"
    backup="${BACKUPS[index]}"
    if [ "${COMMITTED[index]}" -eq 1 ] && [ -e "$destination" ]; then rm -rf -- "$destination"; fi
    if [ "${HAD_EXISTING[index]}" -eq 1 ] && [ -e "$backup" ] && [ ! -e "$destination" ]; then
      mv "$backup" "$destination"
    fi
    if [ -e "$temporary" ]; then rm -rf -- "$temporary"; fi
  done
  if [ -n "$VENV_STAGE" ] && [ -e "$VENV_STAGE" ]; then rm -rf -- "$VENV_STAGE"; fi
}

if [ "$TARGET" = "all" ]; then TARGETS=(agents codex claude copilot openclaw hermes); else TARGETS=("$TARGET"); fi
VENV_STAGE=""
VENV_DIR=""
VENV_BACKUP=""
VENV_HAD_EXISTING=0
VENV_COMMITTED=0

for kind in "${TARGETS[@]}"; do prepare_skill "$kind"; done

if [ "$INSTALL_DEPS" -eq 1 ]; then
  DATA_HOME="$(data_home)"
  if ! mkdir -p "$DATA_HOME"; then rollback; echo "Cannot create runtime directory: $DATA_HOME" >&2; exit 1; fi
  VENV_DIR="$DATA_HOME/venv"
  VENV_STAGE="$(mktemp -d "$DATA_HOME/.venv.install.XXXXXX")"
  if ! "$PYTHON_BIN" -m venv "$VENV_STAGE"; then
    rollback
    echo "Failed to create a virtual environment. On Debian/Ubuntu install python3-venv, then retry." >&2
    exit 1
  fi
  if ! "$VENV_STAGE/bin/python" -m pip install --disable-pip-version-check -r "$SOURCE_DIR/requirements.txt"; then
    rollback
    echo "Failed to install Python dependencies; previous installations were not changed." >&2
    exit 1
  fi
  VENV_BACKUP="$(backup_path "$VENV_DIR")"
  if [ -e "$VENV_DIR" ]; then VENV_HAD_EXISTING=1; fi
fi

for ((index=0; index<${#KINDS[@]}; index++)); do
  if [ "${HAD_EXISTING[index]}" -eq 1 ]; then
    if ! mv "${DESTINATIONS[index]}" "${BACKUPS[index]}"; then rollback; exit 1; fi
  fi
  if ! mv "${TEMPORARIES[index]}" "${DESTINATIONS[index]}"; then
    rollback
    echo "Installation failed; previous installations were restored." >&2
    exit 1
  fi
  COMMITTED[index]=1
done

if [ "$INSTALL_DEPS" -eq 1 ]; then
  if [ "$VENV_HAD_EXISTING" -eq 1 ] && ! mv "$VENV_DIR" "$VENV_BACKUP"; then rollback; exit 1; fi
  if ! mv "$VENV_STAGE" "$VENV_DIR"; then rollback; exit 1; fi
  VENV_COMMITTED=1
  echo "Created isolated runtime at $VENV_DIR"
else
  echo "Skipped dependency installation; commands other than setup require requests, beautifulsoup4, and curl_cffi in the selected Python runtime."
fi

for ((index=0; index<${#KINDS[@]}; index++)); do
  if [ "${HAD_EXISTING[index]}" -eq 1 ]; then echo "Backed up existing ${KINDS[index]} installation to ${BACKUPS[index]}"; fi
  echo "Installed ${KINDS[index]} skill at ${DESTINATIONS[index]}"
done
if ! command -v lark-cli >/dev/null 2>&1; then
  echo "Feishu sync is disabled until @larksuite/cli is installed and authenticated."
fi
echo "Installation complete. Restart or open your Agent, then say:"
echo '  "配置微信公众号文章订阅"'
echo "The Agent will guide configuration in dialogue; do not paste credentials into shell arguments."
