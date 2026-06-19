#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# Steve Bot (SB) — One-Click Installer
# Target: Oracle Cloud Free Tier (Ampere A1 / Ubuntu 22.04+)
# Idempotent — safe to re-run for updates
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

SB_DIR="/home/ubuntu/stevebot"
VENV_DIR="$SB_DIR/venv"
CONFIG_FILE="$SB_DIR/config.json"
LOG_DIR="$SB_DIR/logs"
TRADES_DIR="$SB_DIR/trades"
PERF_DIR="$SB_DIR/performance"
OPENCLAW_DIR="$HOME/.openclaw"

banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════╗"
    echo "  ║         🦈  Steve Bot Installer  🦈          ║"
    echo "  ║     AI Trading Coordinator — Self-Hosted      ║"
    echo "  ╚══════════════════════════════════════════════╝"
    echo -e "${NC}"
}

info()    { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; }
step()    { echo -e "\n${BOLD}${BLUE}──▶ $1${NC}"; }
detail()  { echo -e "    $1"; }

# ── Pre-flight checks ──
preflight() {
    step "Step 1/8: Pre-flight Checks"
    
    if [[ "$(uname -s)" != "Linux" ]]; then
        error "This installer is designed for Linux (Ubuntu on Oracle Cloud)."
        error "Detected: $(uname -s)"
        exit 1
    fi
    
    if [[ "$EUID" -eq 0 ]]; then
        warn "Running as root. Switching to ubuntu user..."
        exec su - ubuntu -c "bash $0"
    fi
    
    if [[ "$(whoami)" != "ubuntu" ]]; then
        warn "Expected user 'ubuntu', running as '$(whoami)' — this may work but YMMV."
    fi
    
    # Check minimum RAM
    local total_ram_mb
    total_ram_mb=$(free -m | awk '/^Mem:/{print $2}')
    if [[ "$total_ram_mb" -lt 2000 ]]; then
        error "Minimum 2GB RAM required. Detected: ${total_ram_mb}MB"
        error "Oracle Ampere A1 gives 24GB free. Recreate your VM with the A1 shape."
        exit 1
    fi
    info "RAM: ${total_ram_mb}MB ✓"
    
    # Check disk space
    local disk_free_gb
    disk_free_gb=$(df -BG /home | awk 'NR==2{print $4}' | sed 's/G//')
    if [[ "$disk_free_gb" -lt 10 ]]; then
        error "Minimum 10GB free disk space. Found: ${disk_free_gb}GB"
        exit 1
    fi
    info "Disk free: ${disk_free_gb}GB ✓"
    
    info "Pre-flight checks passed"
}

# ── Install system packages ──
install_packages() {
    step "Step 2/8: System Packages"
    
    export DEBIAN_FRONTEND=noninteractive
    
    sudo apt-get update -qq
    
    local packages=(python3 python3-pip python3-venv git curl wget jq ufw)
    local to_install=()
    
    for pkg in "${packages[@]}"; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            to_install+=("$pkg")
        fi
    done
    
    if [[ ${#to_install[@]} -gt 0 ]]; then
        detail "Installing: ${to_install[*]}"
        sudo apt-get install -y -qq "${to_install[@]}"
    else
        detail "All packages already installed"
    fi
    
    # Python version check
    local py_ver
    py_ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local py_major
    py_major=$(echo "$py_ver" | cut -d. -f1)
    local py_minor
    py_minor=$(echo "$py_ver" | cut -d. -f2)
    
    if [[ "$py_major" -lt 3 ]] || { [[ "$py_major" -eq 3 ]] && [[ "$py_minor" -lt 9 ]]; }; then
        error "Python 3.10+ required. Found: $py_ver"
        error "On Ubuntu 22.04+: sudo apt install python3.10"
        exit 1
    fi
    info "Python $py_ver ✓"
}

# ── Set up firewall ──
setup_firewall() {
    step "Step 3/8: Firewall"
    
    # OpenClaw gateway listens on 18789 by default
    sudo ufw --force reset > /dev/null 2>&1 || true
    sudo ufw default deny incoming > /dev/null 2>&1
    sudo ufw default allow outgoing > /dev/null 2>&1
    sudo ufw allow ssh > /dev/null 2>&1
    # Only allow loopback for OpenClaw gateway (not exposed to internet)
    # Telegram bot communicates OUTBOUND via webhook or polling
    sudo ufw --force enable > /dev/null 2>&1
    
    info "Firewall configured (SSH only inbound, all outbound allowed)"
}

# ── Create directory structure ──
create_dirs() {
    step "Step 4/8: Directory Structure"
    
    mkdir -p "$SB_DIR"/{trading_engine,systemd,cron,logs,trades,performance}
    mkdir -p "$OPENCLAW_DIR"
    
    info "Directory structure created at $SB_DIR"
}

# ── Set up Python virtual environment ──
setup_venv() {
    step "Step 5/8: Python Environment"
    
    if [[ -d "$VENV_DIR" ]]; then
        detail "Virtual environment exists — updating..."
    else
        python3 -m venv "$VENV_DIR"
        detail "Virtual environment created"
    fi
    
    # shellcheck disable=SC1090
    source "$VENV_DIR/bin/activate"
    
    pip install --upgrade pip -q
    pip install -q requests pyyaml websocket-client python-telegram-bot
    
    info "Python environment ready"
}

# ── Install OpenClaw gateway ──
install_openclaw() {
    step "Step 6/8: OpenClaw Gateway"
    
    if command -v openclaw &>/dev/null; then
        local current_ver
        current_ver=$(openclaw --version 2>/dev/null || echo "unknown")
        detail "OpenClaw already installed ($current_ver)"
        return
    fi
    
    # Node.js check
    if ! command -v node &>/dev/null; then
        detail "Installing Node.js..."
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - > /dev/null 2>&1
        sudo apt-get install -y -qq nodejs
    fi
    
    local node_ver
    node_ver=$(node --version)
    detail "Node.js $node_ver"
    
    detail "Installing OpenClaw..."
    npm install -g openclaw > /dev/null 2>&1
    
    info "OpenClaw gateway installed"
}

# ── Run setup wizard ──
run_wizard() {
    step "Step 7/8: Setup Wizard"
    
    # shellcheck disable=SC1090
    source "$VENV_DIR/bin/activate"
    
    if [[ -f "$SB_DIR/setup_wizard.py" ]]; then
        python3 "$SB_DIR/setup_wizard.py"
    else
        warn "setup_wizard.py not found — skipping interactive setup."
        warn "You'll need to create $CONFIG_FILE manually."
    fi
    
    # Validate config was created
    if [[ -f "$CONFIG_FILE" ]]; then
        chmod 600 "$CONFIG_FILE"
        info "Config created and secured (600 permissions)"
    else
        warn "No config.json created — system won't start until configured."
    fi
}

# ── Install systemd service & cron ──
install_services() {
    step "Step 8/8: Services & Cron"
    
    # ── Systemd service ──
    local SERVICE_FILE="/etc/systemd/system/stevebot.service"
    
    if [[ -f "$SB_DIR/systemd/stevebot.service" ]]; then
        sudo cp "$SB_DIR/systemd/stevebot.service" "$SERVICE_FILE"
        sudo systemctl daemon-reload
        sudo systemctl enable stevebot.service
        detail "systemd service installed (stevebot.service)"
    else
        warn "stevebot.service template not found"
    fi
    
    # ── Cron job for updates ──
    local CRON_ENTRY="0 */6 * * * cd $SB_DIR && $VENV_DIR/bin/python3 $SB_DIR/update_checker.py >> $SB_DIR/logs/update_check.log 2>&1"
    
    # Remove any existing stevebot cron entries
    local existing
    existing=$(crontab -l 2>/dev/null || true)
    if echo "$existing" | grep -q "update_checker.py"; then
        # Update existing entry
        local new_crontab
        new_crontab=$(echo "$existing" | grep -v "update_checker.py")
        new_crontab="$new_crontab"$'\n'"$CRON_ENTRY"
        echo "$new_crontab" | crontab -
        detail "Cron job updated"
    else
        (echo "$existing"; echo "$CRON_ENTRY") | crontab -
        detail "Cron job installed (every 6 hours)"
    fi
    
    # ── Log rotation ──
    local LOGROTATE_CONF="/etc/logrotate.d/stevebot"
    sudo tee "$LOGROTATE_CONF" > /dev/null << 'LOGROTATE'
/home/ubuntu/stevebot/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 50M
}
LOGROTATE
    detail "Log rotation configured"
    
    info "All services installed"
}

# ── Final summary ──
print_summary() {
    echo ""
    echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}${BOLD}║      🦈  Steve Bot Installation Complete!  🦈       ║${NC}"
    echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BOLD}What's installed:${NC}"
    echo -e "  ├─ Steve Bot engine:     ${CYAN}$SB_DIR${NC}"
    echo -e "  ├─ Config:               ${CYAN}$CONFIG_FILE${NC}"
    echo -e "  ├─ Virtual env:          ${CYAN}$VENV_DIR${NC}"
    echo -e "  ├─ Logs:                 ${CYAN}$LOG_DIR${NC}"
    echo -e "  └─ Trades/Performance:   ${CYAN}$TRADES_DIR${NC} / $PERF_DIR${NC}"
    echo ""
    echo -e "  ${BOLD}Commands:${NC}"
    echo -e "  ├─ Start SB:             ${YELLOW}sudo systemctl start stevebot${NC}"
    echo -e "  ├─ Stop SB:              ${YELLOW}sudo systemctl stop stevebot${NC}"
    echo -e "  ├─ Status:               ${YELLOW}sudo systemctl status stevebot${NC}"
    echo -e "  ├─ View logs:            ${YELLOW}tail -f $LOG_DIR/sb_*.log${NC}"
    echo -e "  └─ Re-run wizard:        ${YELLOW}python3 $SB_DIR/setup_wizard.py${NC}"
    echo ""
    echo -e "  ${BOLD}Next Steps:${NC}"
    echo -e "  1. Message your bot on Telegram: /start"
    echo -e "  2. SB will greet you by name and confirm paper trading mode"
    echo -e "  3. Use /help to see available commands"
    echo -e "  4. Read $SB_DIR/README.md for the full guide"
    echo ""
    echo -e "  ${BOLD}Costs:${NC}"
    echo -e "  ├─ Oracle VM:            ${GREEN}FREE${NC} (Ampere A1 4 OCPU / 24GB)"
    echo -e "  ├─ DeepSeek Chat API:    ~${YELLOW}\$3-5/month${NC}"
    echo -e "  └─ Bitget API:           ${GREEN}FREE${NC}"
    echo ""
    echo -e "  ${BOLD}Security:${NC}"
    echo -e "  ├─ Config file:          ${GREEN}chmod 600${NC} (owner-only)"
    echo -e "  ├─ Firewall:             ${GREEN}SSH only inbound${NC}"
    echo -e "  └─ API keys:             ${GREEN}No withdrawal permissions${NC}"
    echo ""
    echo -e "  ${CYAN}Happy trading! 🦈${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

banner

preflight
install_packages
setup_firewall
create_dirs
setup_venv
install_openclaw
run_wizard
install_services
print_summary
