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

systemctl disable --now shut-down.timer shut-down-init.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/shut-down-init.service
rm -f /etc/systemd/system/shut-down.service
rm -f /etc/systemd/system/shut-down.timer
rm -f /etc/sudoers.d/shut-down
rm -f /etc/shut-down.conf
rm -f /usr/local/bin/off
rm -f /usr/local/libexec/shut-down
rm -rf /run/shut-down
systemctl daemon-reload

if [[ ${purge_state} == true ]]; then
  rm -rf /var/lib/shut-down
fi

printf 'Uninstalled shut-down.\n'
