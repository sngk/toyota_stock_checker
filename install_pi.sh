#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
service_user="$(id -un)"
service_name="prado-watch"
update_marker="/tmp/prado-watch-update-${service_user}"

echo "WA Prado Watch Raspberry Pi installer"
echo "Repository: $repo_dir"
echo "Service user: $service_user"

if ! python_bin="$(command -v python3)"; then
  echo "ERROR: python3 is not installed."
  echo "Run: sudo apt update && sudo apt install -y git python3 python3-venv"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is not installed."
  echo "Run: sudo apt update && sudo apt install -y git python3 python3-venv"
  exit 1
fi

echo "[1/5] Creating Python environment..."
"$python_bin" -m venv "$repo_dir/.venv"
echo "[2/5] Installing Python packages (this can take a few minutes)..."
"$repo_dir/.venv/bin/pip" install --upgrade pip
"$repo_dir/.venv/bin/pip" install -r "$repo_dir/requirements.txt"

if [[ ! -f "$repo_dir/.env" ]]; then
  echo "[3/5] Configuring Discord..."
  read -r -s -p "Discord webhook URL (leave blank to disable Discord): " webhook_url
  echo
  {
    printf 'DISCORD_WEBHOOK_URL=%s\n' "$webhook_url"
    printf 'PRADO_HOST=0.0.0.0\n'
    printf 'PRADO_PORT=443\n'
  } > "$repo_dir/.env"
  chmod 600 "$repo_dir/.env"
else
  echo "[3/5] Existing Discord configuration found; keeping it."
fi

echo "[4/5] Installing background services (sudo may ask for your password)..."
sudo tee "/etc/systemd/system/${service_name}.service" >/dev/null <<EOF
[Unit]
Description=WA Prado stock watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
WorkingDirectory=${repo_dir}
EnvironmentFile=-${repo_dir}/.env
ExecStart=${repo_dir}/.venv/bin/python ${repo_dir}/app.py
Restart=on-failure
RestartSec=10
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee "/etc/systemd/system/${service_name}-update.service" >/dev/null <<EOF
[Unit]
Description=Update WA Prado stock watcher from Git
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/runuser -u ${service_user} -- /bin/bash ${repo_dir}/update_pi.sh ${update_marker}
ExecStartPost=/bin/bash -c 'if test -f "${update_marker}"; then rm -f "${update_marker}"; systemctl restart ${service_name}.service; fi'
EOF

sudo tee "/etc/systemd/system/${service_name}-update.timer" >/dev/null <<EOF
[Unit]
Description=Check for WA Prado watcher updates

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
echo "[5/5] Starting the app and automatic updater..."
sudo systemctl enable --now "${service_name}.service" "${service_name}-update.timer"

lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Installed and running."
echo "Dashboard: http://${lan_ip:-RASPBERRY_PI_IP}:443"
echo "Status: sudo systemctl status ${service_name}"
echo "Logs:   journalctl -u ${service_name} -f"
