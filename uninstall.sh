#!/usr/bin/env bash
set -euo pipefail

purge_state=false
if [[ ${1:-} == --purge-state ]]; then
  purge_state=true
elif [[ $# -gt 0 ]]; then
  printf 'Usage: sudo ./uninstall.sh [--purge-state]\n' >&2
  exit 2
fi

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run this uninstaller with sudo.\n' >&2
  exit 1
fi

systemctl disable --now devbox-power.timer devbox-power-init.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/devbox-power-init.service
rm -f /etc/systemd/system/devbox-power.service
rm -f /etc/systemd/system/devbox-power.timer
rm -f /etc/sudoers.d/devbox-power
rm -f /etc/devbox-power.conf
rm -f /usr/local/bin/off
rm -f /usr/local/bin/devbox-power
rm -f /usr/local/libexec/devbox-power
rm -rf /run/devbox-power
systemctl daemon-reload

if [[ ${purge_state} == true ]]; then
  rm -rf /var/lib/devbox-power
fi

printf 'Uninstalled devbox-power.\n'
