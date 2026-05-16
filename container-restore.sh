#!/usr/bin/env bash
# container-restore.sh — Restore manually-installed container customizations
# Run this inside a freshly recreated container as user ubuntu.
# Generated: 2026-05-15

set -euo pipefail
IFS=$'\n\t'

# ─────────────────────────────────────────────────────────
echo "=== 1. Apt packages ==="
# ─────────────────────────────────────────────────────────
sudo apt-get update -qq

sudo apt-get install -y \
    aggregate \
    bison \
    build-essential \
    ca-certificates \
    ccache \
    cmake \
    curl \
    dnsutils \
    flex \
    fzf \
    gh \
    git \
    git-delta \
    gnupg2 \
    gperf \
    iproute2 \
    ipset \
    iptables \
    jq \
    less \
    libffi-dev \
    libssl-dev \
    libusb-1.0-0-dev \
    nano \
    ninja-build \
    nodejs \
    openssh-client \
    poppler-utils \
    python3 \
    python3-pip \
    python3-serial \
    python3-venv \
    unzip \
    vim \
    wget \
    zsh

# ─────────────────────────────────────────────────────────
echo "=== 2. Oh-My-Zsh + Powerlevel10k ==="
# ─────────────────────────────────────────────────────────
if [ ! -d "$HOME/.oh-my-zsh" ]; then
    RUNZSH=no CHSH=no sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
fi

if [ ! -d "$HOME/.oh-my-zsh/themes/powerlevel10k" ]; then
    git clone --depth=1 https://github.com/romkatv/powerlevel10k.git \
        "$HOME/.oh-my-zsh/themes/powerlevel10k"
fi

# ─────────────────────────────────────────────────────────
echo "=== 3. Configure .zshrc ==="
# ─────────────────────────────────────────────────────────
cat > "$HOME/.zshrc" << 'ZSHRC_EOF'
export LANG='en_US.UTF-8'
export LANGUAGE='en_US:en'
export LC_ALL='en_US.UTF-8'
export TERM=xterm

##### Zsh/Oh-my-Zsh Configuration
export ZSH="/home/ubuntu/.oh-my-zsh"

ZSH_THEME="powerlevel10k/powerlevel10k"
plugins=(git fzf )


[[ -r /usr/share/doc/fzf/examples/key-bindings.zsh ]] && source /usr/share/doc/fzf/examples/key-bindings.zsh
[[ -r /usr/share/doc/fzf/examples/completion.zsh ]] && source /usr/share/doc/fzf/examples/completion.zsh
export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history
export PATH="$HOME/.local/bin:$PATH"
source $ZSH/oh-my-zsh.sh
POWERLEVEL9K_SHORTEN_STRATEGY="truncate_to_last"
POWERLEVEL9K_LEFT_PROMPT_ELEMENTS=(user dir vcs status)
POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS=()
POWERLEVEL9K_STATUS_OK=false
POWERLEVEL9K_STATUS_CROSS=true

# ESP-IDF
[[ -f "$HOME/esp-idf/export.sh" ]] && source "$HOME/esp-idf/export.sh" > /dev/null 2>&1
ZSHRC_EOF

# ─────────────────────────────────────────────────────────
echo "=== 4. pip packages (user install) ==="
# ─────────────────────────────────────────────────────────
pip3 install --user --break-system-packages \
    esptool==5.2.0 \
    git-filter-repo==2.47.0 \
    psycopg2-binary==2.9.12 \
    python-dotenv==1.2.2 \
    SQLAlchemy==2.0.49

# ─────────────────────────────────────────────────────────
echo "=== 5. npm global packages ==="
# ─────────────────────────────────────────────────────────
npm install -g @anthropic-ai/claude-code@2.1.114

# ─────────────────────────────────────────────────────────
echo "=== 6. ESP-IDF v6.0.1 (~1 hour, be patient) ==="
# ─────────────────────────────────────────────────────────
if [ ! -d "$HOME/esp-idf" ]; then
    git clone --recursive --branch v6.0.1 \
        https://github.com/espressif/esp-idf.git "$HOME/esp-idf"
fi

if [ ! -d "$HOME/.espressif/python_env/idf6.0_py3.13_env" ]; then
    "$HOME/esp-idf/install.sh" esp32s3
fi

# ─────────────────────────────────────────────────────────
echo "=== 7. Restore /usr/local/bin/init-firewall.sh ==="
# ─────────────────────────────────────────────────────────
sudo tee /usr/local/bin/init-firewall.sh > /dev/null << 'FIREWALL_EOF'
#!/bin/bash
set -uo pipefail   # Undefined vars + pipeline failures, but NO -e so individual
IFS=$'\n\t'       # command failures don't abort the entire firewall setup.

# This script mirrors the upstream Claude Code devcontainer firewall helper.
# It is NOT run automatically; invoke explicitly when you want to restrict
# outbound connectivity to common development endpoints.

# 1. Extract Docker DNS info BEFORE any flushing
DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)

# Flush existing rules and delete existing ipsets
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# 2. Selectively restore ONLY internal Docker DNS resolution
if [ -n "$DOCKER_DNS_RULES" ]; then
    echo "Restoring Docker DNS rules..."
    iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
    iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
    echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
else
    echo "No Docker DNS rules to restore"
fi

# First allow DNS and localhost before any restrictions
# Allow outbound DNS
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
# Allow inbound DNS responses
iptables -A INPUT -p udp --sport 53 -j ACCEPT
# Allow outbound SSH
iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT
# Allow inbound SSH responses
iptables -A INPUT -p tcp --sport 22 -m state --state ESTABLISHED -j ACCEPT
# Allow localhost
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Create ipset with CIDR support
ipset create allowed-domains hash:net

# Fetch GitHub meta information and aggregate + add their IP ranges
echo "Fetching GitHub IP ranges..."
gh_ranges=$(curl -s https://api.github.com/meta)
if [ -z "$gh_ranges" ]; then
    echo "WARNING: Failed to fetch GitHub IP ranges — GitHub access may not work"
    gh_ranges=''
fi

if [ -n "$gh_ranges" ] && ! echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null; then
    echo "WARNING: GitHub API response missing required fields — GitHub access may not work"
    gh_ranges=''
fi

if [ -n "$gh_ranges" ]; then
  echo "Processing GitHub IPs..."
  while read -r cidr; do
      if [[ ! "$cidr" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
          echo "WARNING: Skipping invalid CIDR from GitHub meta: $cidr"
          continue
      fi
      echo "Adding GitHub range $cidr"
      ipset add allowed-domains "$cidr" || true
  done < <(echo "$gh_ranges" | jq -r '(.web + .api + .git)[]' | aggregate -q)
fi

# Resolve and add other allowed domains
for domain in \
    "registry.npmjs.org" \
    "api.anthropic.com" \
    "sentry.io" \
    "statsig.anthropic.com" \
    "statsig.com" \
    "marketplace.visualstudio.com" \
    "vscode.blob.core.windows.net" \
    "update.code.visualstudio.com"; do
    echo "Resolving $domain..."
    ips=$(dig +noall +answer A "$domain" | awk '$4 == "A" {print $5}')
    if [ -z "$ips" ]; then
        echo "WARNING: Failed to resolve $domain — skipping"
        continue
    fi

    while read -r ip; do
        if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            echo "WARNING: Skipping invalid IP from DNS for $domain: $ip"
            continue
        fi
        echo "Adding $ip for $domain"
        ipset add allowed-domains "$ip" || true
    done < <(echo "$ips")
done

# Get host IP from default route
HOST_IP=$(ip route | grep default | cut -d" " -f3)
if [ -z "$HOST_IP" ]; then
    echo "WARNING: Failed to detect host IP — host network access may not work"
    HOST_IP="172.17.0.1"  # Docker default gateway fallback
fi

HOST_NETWORK=$(echo "$HOST_IP" | sed "s/\.[0-9]*$/.0\/24/")
echo "Host network detected as: $HOST_NETWORK"

# Set up remaining iptables rules
iptables -A INPUT -s "$HOST_NETWORK" -j ACCEPT
iptables -A OUTPUT -d "$HOST_NETWORK" -j ACCEPT

# Set default policies to DROP first
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# First allow established connections for already approved traffic
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Then allow only specific outbound traffic to allowed domains
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT

# Explicitly REJECT all other outbound traffic for immediate feedback
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

echo "Firewall configuration complete"

# Optional quick verification (non-fatal)
if curl --connect-timeout 3 https://example.com >/dev/null 2>&1; then
    echo "WARNING: Firewall may not be blocking — example.com was reachable"
else
    echo "Firewall OK — example.com blocked as expected"
fi

if curl --connect-timeout 3 https://api.github.com/zen >/dev/null 2>&1; then
    echo "Firewall OK — api.github.com reachable as expected"
else
    echo "WARNING: api.github.com not reachable — GitHub access may not work"
fi
FIREWALL_EOF
sudo chmod +x /usr/local/bin/init-firewall.sh

# ─────────────────────────────────────────────────────────
echo "=== 8. Git config ==="
# ─────────────────────────────────────────────────────────
git config --global user.name "development"
git config --global user.email "pmsfriend@googlemail.com"
git config --global core.autocrlf input
git config --global credential."https://github.com".helper "!/usr/bin/gh auth git-credential"
git config --global credential."https://gist.github.com".helper "!/usr/bin/gh auth git-credential"

# ─────────────────────────────────────────────────────────
echo "=== 9. Set zsh as default shell ==="
# ─────────────────────────────────────────────────────────
if [ "$(getent passwd ubuntu | cut -d: -f7)" != "$(which zsh)" ]; then
    sudo chsh -s "$(which zsh)" ubuntu
fi

# ─────────────────────────────────────────────────────────
echo ""
echo "=== DONE ==="
echo ""
echo "Manual steps remaining:"
echo "  1. Authenticate GitHub CLI:      gh auth login"
echo "  2. Authenticate Claude Code:     ~/.claude survives rebuild — should auto-login"
echo "  3. Restore Powerlevel10k config: ~/.p10k.zsh is LOST — run 'p10k configure' or"
echo "                                   restore from a backup if you have one"
echo "  4. VSCode: reconnect container — .vscode-server will reinstall extensions (~1GB)"
echo "  5. Verify ESP-IDF:               source ~/esp-idf/export.sh && idf.py --version"
echo ""
