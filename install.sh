#!/usr/bin/env bash
set -euo pipefail

timezone=America/Los_Angeles
default_stop_hour=19
notify_before_minutes=30
terminal_user=${SUDO_USER:-ubuntu}
reset_state=false

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

Options:
  --timezone ZONE          IANA timezone (default: America/Los_Angeles)
  --default-stop-hour HOUR Whole local hour, 0-23 (default: 19)
  --notify-before MINUTES  Reminder lead time (default: 30)
  --terminal-user USER     User whose PTYs receive reminders (default: invoking user)
  --reset-state            Replace an existing future deadline with the default
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timezone)
      timezone=${2:?Missing value for --timezone}
      shift 2
      ;;
    --default-stop-hour)
      default_stop_hour=${2:?Missing value for --default-stop-hour}
      shift 2
      ;;
    --notify-before)
      notify_before_minutes=${2:?Missing value for --notify-before}
      shift 2
      ;;
    --terminal-user)
      terminal_user=${2:?Missing value for --terminal-user}
      shift 2
      ;;
    --reset-state)
      reset_state=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run this installer with sudo.\n' >&2
  exit 1
fi

if [[ ! ${default_stop_hour} =~ ^([0-9]|1[0-9]|2[0-3])$ ]]; then
  printf 'Default stop hour must be between 0 and 23.\n' >&2
  exit 2
fi
if [[ ! ${notify_before_minutes} =~ ^[1-9][0-9]*$ ]] || (( notify_before_minutes > 1440 )); then
  printf 'Notify-before must be between 1 and 1440 minutes.\n' >&2
  exit 2
fi
if ! id "${terminal_user}" >/dev/null 2>&1; then
  printf 'Terminal user does not exist: %s\n' "${terminal_user}" >&2
  exit 2
fi
if ! python3 -c 'from zoneinfo import ZoneInfo; import sys; ZoneInfo(sys.argv[1])' "${timezone}"; then
  printf 'Unknown IANA timezone: %s\n' "${timezone}" >&2
  exit 2
fi
if ! command -v systemctl >/dev/null; then
  printf 'systemd is required.\n' >&2
  exit 1
fi
if ! command -v visudo >/dev/null; then
  printf 'visudo is required. Install the sudo package first.\n' >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

install -d -m 0755 /usr/local/libexec /usr/local/bin
install -d -m 0700 /var/lib/shut-down
install -m 0755 "${repo_root}/src/shut_down.py" /usr/local/libexec/shut-down
install -m 0755 "${repo_root}/src/off" /usr/local/bin/off
install -m 0644 "${repo_root}/systemd/shut-down-init.service" /etc/systemd/system/shut-down-init.service
install -m 0644 "${repo_root}/systemd/shut-down.service" /etc/systemd/system/shut-down.service
install -m 0644 "${repo_root}/systemd/shut-down.timer" /etc/systemd/system/shut-down.timer

config_temp=$(mktemp)
sudoers_temp=$(mktemp)
cleanup() {
  rm -f "${config_temp}" "${sudoers_temp}"
}
trap cleanup EXIT

cat >"${config_temp}" <<EOF
# Managed by the shut-down installer.
TIMEZONE=${timezone}
DEFAULT_STOP_HOUR=${default_stop_hour}
NOTIFY_BEFORE_MINUTES=${notify_before_minutes}
TERMINAL_USERS=${terminal_user}
STATE_FILE=/var/lib/shut-down/state.json
LOCK_FILE=/run/shut-down/state.lock
EOF
install -m 0644 "${config_temp}" /etc/shut-down.conf

cat >"${sudoers_temp}" <<EOF
Cmnd_Alias SHUT_DOWN = /usr/local/libexec/shut-down status, /usr/local/libexec/shut-down delay *, /usr/local/libexec/shut-down stop-at *, /usr/local/libexec/shut-down reset, /usr/local/libexec/shut-down notify-test
${terminal_user} ALL=(root) NOPASSWD: SHUT_DOWN
EOF
visudo -cf "${sudoers_temp}" >/dev/null
install -m 0440 "${sudoers_temp}" /etc/sudoers.d/shut-down

if [[ ${reset_state} == true ]]; then
  rm -f /var/lib/shut-down/state.json
fi

systemctl daemon-reload
systemctl enable shut-down-init.service shut-down.timer >/dev/null
systemctl restart shut-down-init.service
systemctl start shut-down.timer

printf '\nInstalled off.\n'
/usr/local/libexec/shut-down status
printf '\nTest the terminal reminder with: off notify-test\n'
