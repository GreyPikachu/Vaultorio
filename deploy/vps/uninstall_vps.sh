#!/usr/bin/env bash
# ==============================================================================
# Factorio Safe Hub & STUN Coordinator - VPS Uninstallation Script
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
    echo -e "${BLUE}[INFO]${NC} $(date +'%Y-%m-%d %H:%M:%S') $1"
}

log_ok() {
    echo -e "${GREEN}[OK]${NC} $(date +'%Y-%m-%d %H:%M:%S') $1"
}

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR]${NC} Must be run as root (sudo bash uninstall_vps.sh)" >&2
    exit 1
fi

log_info "Stopping and disabling factorio-hub systemd service..."
systemctl stop factorio-hub.service 2>/dev/null || true
systemctl disable factorio-hub.service 2>/dev/null || true

SERVICE_FILE="/etc/systemd/system/factorio-hub.service"
if [[ -f "${SERVICE_FILE}" ]]; then
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    log_ok "Removed ${SERVICE_FILE}."
fi

# Remove UFW rules if active
if command -v ufw >/dev/null 2>&1; then
    ufw delete allow 8088/tcp 2>/dev/null || true
    ufw delete allow 14197/udp 2>/dev/null || true
    log_ok "Removed UFW rules for 8088/tcp and 14197/udp."
fi

INSTALL_DIR="/opt/factorio-hub"
if [[ -d "${INSTALL_DIR}" ]]; then
    log_info "Preserving save backups in ${INSTALL_DIR}/data..."
    rm -rf "${INSTALL_DIR}/venv" "${INSTALL_DIR}/src"
    log_ok "Factorio Hub service binary removed. World saves preserved in ${INSTALL_DIR}/data."
fi

echo -e "${GREEN}Factorio Hub uninstallation complete.${NC}"
