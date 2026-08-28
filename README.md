# WA Prado Watch

Checks WA Toyota dealer pages for new and demonstrator LandCruiser Prado stock every hour. The dashboard tracks new listings, clearly labels demos, and highlights GX models and preferred colours.

## Run

```powershell
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8080`. For private phone access through Tailscale, run `start_with_tailscale.ps1` and open the address it prints.

Dealer pages can be changed in `dealers.json`. Each dealer's `/demonstrators/prado` page is checked automatically; `demo_url` can override it when necessary.

## Discord alerts

Create a webhook in the desired Discord channel, then start the app. It will securely prompt for the webhook URL:

```powershell
python app.py
```

The first Discord-enabled scan posts every Prado currently in stock. Later scans post only newly appearing or returning cars. The entered URL is kept only in memory. Keep it private; do not add it to the repository.
