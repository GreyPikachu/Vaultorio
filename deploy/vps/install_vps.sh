#!/usr/bin/env bash
# ==============================================================================
# Factorio Safe Hub & STUN Coordinator - VPS Deployment Script
# Target: Ubuntu/Debian VPS (e.g. <YOUR_VPS_IP> alongside Xray Reality & Hysteria 2)
# ==============================================================================

set -euo pipefail

# Color palette
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $(date +'%Y-%m-%d %H:%M:%S') $1"
}

log_ok() {
    echo -e "${GREEN}[OK]${NC} $(date +'%Y-%m-%d %H:%M:%S') $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $(date +'%Y-%m-%d %H:%M:%S') $1"
}

log_err() {
    echo -e "${RED}[ERROR]${NC} $(date +'%Y-%m-%d %H:%M:%S') $1" >&2
}

cleanup_on_error() {
    log_err "Installation failed on line $1. Exiting without completing setup."
}
trap 'cleanup_on_error $LINENO' ERR

# ------------------------------------------------------------------------------
# 1. Root & Prerequisite Checks
# ------------------------------------------------------------------------------
log_info "Starting Factorio Hub deployment on VPS..."

if [[ $EUID -ne 0 ]]; then
    log_err "This deployment script must be run as root (use: sudo bash install_vps.sh)"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
    log_warn "Python 3 or python3-venv is missing. Installing python3, python3-venv, and python3-pip..."
    apt-get update -y && apt-get install -y python3 python3-venv python3-pip curl
fi

command -v curl >/dev/null 2>&1 || {
    apt-get update -y && apt-get install -y curl
}

# ------------------------------------------------------------------------------
# 2. Critical VPN Safety Barrier Checks
# ------------------------------------------------------------------------------
HTTP_PORT=8088
UDP_STUN_PORT=14197

log_info "Verifying port safety constraints (avoiding VPN & Hysteria conflicts)..."

# Ensure UDP port is NOT inside Hysteria port hopping range (20000:50000)
if (( UDP_STUN_PORT >= 20000 && UDP_STUN_PORT <= 50000 )); then
    log_err "CRITICAL CONFLICT: Port ${UDP_STUN_PORT}/udp is inside the 20000:50000 range intercepted by Hysteria 2!"
    exit 1
fi
log_ok "STUN port ${UDP_STUN_PORT}/udp is outside Hysteria port hopping range."

# Check if ports are already bound by unexpected processes
if command -v ss >/dev/null 2>&1; then
    if ss -tlpn | grep -q ":${HTTP_PORT} "; then
        log_warn "Port ${HTTP_PORT}/tcp appears to be in use. It will be replaced if it belongs to an older factorio-hub."
    fi
    if ss -ulpn | grep -q ":${UDP_STUN_PORT} "; then
        log_warn "Port ${UDP_STUN_PORT}/udp appears to be in use. It will be replaced if it belongs to an older factorio-hub."
    fi
fi

# Check iptables NAT table to ensure no conflicting redirects exist for 14197
if command -v iptables >/dev/null 2>&1; then
    if iptables -t nat -L PREROUTING -n 2>/dev/null | grep -E "dpts?:20000:50000" >/dev/null 2>&1; then
        log_ok "Detected active Hysteria iptables redirection for 20000:50000. Port ${UDP_STUN_PORT} is verified safe."
    fi
fi

# ------------------------------------------------------------------------------
# 3. Directory Layout and Source Installation
# ------------------------------------------------------------------------------
INSTALL_DIR="/opt/factorio-hub"
DATA_DIR="${INSTALL_DIR}/data"
VENV_DIR="${INSTALL_DIR}/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log_info "Setting up application directory at ${INSTALL_DIR}..."
mkdir -p "${DATA_DIR}/backups"
mkdir -p "${DATA_DIR}/tmp"
mkdir -p "${DATA_DIR}/logs"

log_info "Synchronizing application source files..."
# If executed from git workspace, copy files directly
if [[ -d "${REPO_ROOT}/src/server" ]]; then
    mkdir -p "${INSTALL_DIR}/src"
    cp -r "${REPO_ROOT}/src/server" "${INSTALL_DIR}/src/"
    cp -r "${REPO_ROOT}/src/common" "${INSTALL_DIR}/src/"
    cp "${REPO_ROOT}/deploy/vps/requirements.txt" "${INSTALL_DIR}/"
    cp "${REPO_ROOT}/deploy/vps/factorio-hub.service" "${INSTALL_DIR}/"
elif [[ -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    cp -r "${SCRIPT_DIR}/../../src/server" "${INSTALL_DIR}/src/" 2>/dev/null || true
    cp -r "${SCRIPT_DIR}/../../src/common" "${INSTALL_DIR}/src/" 2>/dev/null || true
    cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
    cp "${SCRIPT_DIR}/factorio-hub.service" "${INSTALL_DIR}/"
fi

# ------------------------------------------------------------------------------
# 4. Virtual Environment & Dependencies
# ------------------------------------------------------------------------------
log_info "Configuring Python virtual environment in ${VENV_DIR}..."
if [[ ! -f "${VENV_DIR}/bin/python3" ]]; then
    python3 -m venv "${VENV_DIR}"
fi

log_info "Installing Python dependencies with pip..."
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools --quiet
"${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" --quiet
log_ok "Dependencies installed successfully."

# ------------------------------------------------------------------------------
# 5. Firewall Configuration (UFW)
# ------------------------------------------------------------------------------
if command -v ufw >/dev/null 2>&1; then
    UFW_STATUS=$(ufw status | head -n 1 || true)
    if [[ "${UFW_STATUS}" == *"active"* ]]; then
        log_info "UFW firewall is active. Adding non-conflicting rules for Factorio Hub..."
        ufw allow "${HTTP_PORT}/tcp" comment "Factorio Cloud Save Hub API" >/dev/null
        ufw allow "${UDP_STUN_PORT}/udp" comment "Factorio P2P STUN Coordinator" >/dev/null
        log_ok "UFW rules applied: allowed ${HTTP_PORT}/tcp and ${UDP_STUN_PORT}/udp."
    else
        log_info "UFW is inactive. Skipping firewall rule changes."
    fi
fi

# ------------------------------------------------------------------------------
# 6. Systemd Service Registration and Startup
# ------------------------------------------------------------------------------
SERVICE_DEST="/etc/systemd/system/factorio-hub.service"
log_info "Installing systemd unit to ${SERVICE_DEST}..."
cp "${INSTALL_DIR}/factorio-hub.service" "${SERVICE_DEST}"

systemctl daemon-reload
systemctl enable factorio-hub.service >/dev/null 2>&1
systemctl restart factorio-hub.service

# ------------------------------------------------------------------------------
# 7. Post-Launch Health Verification
# ------------------------------------------------------------------------------
log_info "Verifying service vitality via local health check..."
MAX_ATTEMPTS=10
ATTEMPT=0
HEALTH_OK=0

while (( ATTEMPT < MAX_ATTEMPTS )); do
    sleep 1
    ATTEMPT=$(( ATTEMPT + 1 ))
    if curl -s -f "http://127.0.0.1:${HTTP_PORT}/health" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
done

if (( HEALTH_OK == 1 )); then
    HEALTH_DATA=$(curl -s "http://127.0.0.1:${HTTP_PORT}/health")
    log_ok "Factorio Hub is successfully running and healthy!"
    echo -e "${GREEN}Response:${NC} ${HEALTH_DATA}"
    echo ""
    echo "=========================================================================="
    echo -e "${GREEN}DEPLOYMENT COMPLETED SUCCESSFULLY${NC}"
    echo "Service Status:  systemctl status factorio-hub"
    echo "Service Logs:    journalctl -u factorio-hub -f"
    echo "HTTP API:        http://0.0.0.0:${HTTP_PORT}"
    echo "UDP STUN:        0.0.0.0:${UDP_STUN_PORT}"
    echo "Data Path:       ${DATA_DIR}"
    echo "Memory Limit:    64 MB (strictly enforced by systemd cgroups)"
    echo "=========================================================================="
else
    log_err "Health check timed out after ${MAX_ATTEMPTS} seconds. Inspecting logs:"
    journalctl -u factorio-hub -n 25 --no-pager || true
    exit 1
fi
