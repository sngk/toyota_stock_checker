# WA Prado Watch

Checks WA Toyota dealer pages for LandCruiser Prado stock every hour. The dashboard tracks new vehicles and highlights GX models and preferred colours.

## Run

```powershell
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8080`. For private phone access through Tailscale, run `start_with_tailscale.ps1` and open the address it prints.

Dealer pages can be changed in `dealers.json`.

## Discord alerts

Create a webhook in the desired Discord channel, then set it before starting the app:

```powershell
$env:DISCORD_WEBHOOK_URL = "paste-webhook-url-here"
python app.py
```

The first scan posts every Prado currently in stock. Later scans post only newly appearing or returning cars. Keep the webhook URL private; do not add it to the repository.
