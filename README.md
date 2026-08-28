# WA Prado Watch

Checks WA Toyota dealer pages for LandCruiser Prado stock every eight hours. The dashboard tracks new vehicles and highlights GX models and preferred colours.

## Run

```powershell
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8080`. For private phone access through Tailscale, run `start_with_tailscale.ps1` and open the address it prints.

Dealer pages can be changed in `dealers.json`.
