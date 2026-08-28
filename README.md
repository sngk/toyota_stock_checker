# WA Prado Watch

Checks WA Toyota dealer pages for new and demonstrator LandCruiser Prado stock every hour. The dashboard tracks new listings, clearly labels demos, and highlights GX models and preferred colours.

## Run

```text
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8080` on the same computer. Raspberry Pi installation below enables access from other devices on your home network.

Dealer pages can be changed in `dealers.json`. Each dealer's `/demonstrators/prado` page is checked automatically; `demo_url` can override it when necessary.

## Raspberry Pi

Install Raspberry Pi OS, connect the Pi to your home network, clone this repository, and run:

```bash
cd toyota_stock_checker
sudo apt update
sudo apt install -y git python3 python3-venv
bash install_pi.sh
```

The installer asks for the Discord webhook, runs the app as a `systemd` service on port 8080, and starts it automatically after a reboot. Connect your phone to the same home Wi-Fi and open the local address printed by the installer, such as `http://192.168.1.50:8080`. Port 8080 avoids conflicts with Pi-hole's web server.

This is local-network access only. It does not expose the dashboard to the internet. For a stable bookmark, reserve the Pi's IP address in your router's DHCP settings, or try `http://raspberrypi.local:443` if your network supports local hostnames.

It also checks the current Git branch every 15 minutes. A fast-forward update is pulled automatically, dependencies are refreshed, and the app restarts. Updates are skipped if tracked files were edited locally or the branch history diverged, so local work is not overwritten.

Useful commands:

```bash
sudo systemctl status prado-watch
journalctl -u prado-watch -f
sudo systemctl restart prado-watch
sudo systemctl start prado-watch-update.service
```

## Discord alerts

Create a webhook in the desired Discord channel, then start the app. It will securely prompt for the webhook URL:

```text
python app.py
```

The first Discord-enabled scan posts every Prado currently in stock. Later scans post only newly appearing or returning cars. The entered URL is kept only in memory. Keep it private; do not add it to the repository.
