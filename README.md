# Wifi-portal voor Omada (PPSK)

Kleine webportal om wifi-apparaten te beheren via de **Omada Open API**: elk apparaat een eigen
PPSK-sleutel, gekoppeld aan één MAC-adres, in het juiste VLAN. Alles (sites, SSID's, VLAN's,
PPSK-profielen) komt uit Omada zelf; niets handmatig configureren.

## Wat het kan

- **Nieuwe sleutel** met QR-code; landt in het gast-VLAN tot je hem koppelt. Niet gekoppeld binnen
  X uur (instelbaar) → vervalt vanzelf.
- **Koppelen**: per wachtende sleutel de apparaten die sindsdien in het gast-VLAN verbonden, nieuwste
  eerst. Eén sleutel = maximaal één MAC-adres.
- **Gekoppelde sleutels per VLAN** (inklapbaar): VLAN wijzigen, nieuwe sleutel, ontkoppelen, verwijderen.
- **Gedeelde sleutels** worden nooit gewijzigd of verwijderd; per gebruiker kun je een eigen privé-sleutel maken.
- **Alle apparaten** (draadloos en bekabeld, per VLAN): hernoemen, vast IP, VLAN wijzigen
  (draadloos via de sleutel, bekabeld via de switchpoort, alleen als dat veilig kan), type/merk/model en icoon.
- **Privé-MAC** (willekeurig MAC-adres): nooit een vast IP; sleutels verlopen na X uur.
- **Eigen database** (SQLite) met inventaris en logboek; namen en type die Omada vergeet worden teruggezet.
- **API voor de Homey-widget** (`/api/v1/new`, `/api/v1/key/<naam>`, Bearer-token uit Instellingen).

## Installeren (Docker)

Je hebt alleen `docker-compose.yml` en een `.env` nodig; het image wordt rechtstreeks uit deze
GitHub-repo gebouwd (`build.context: https://github.com/W3BNVR/wifi-portal.git#main:portal`).

```sh
cp .env.example .env      # invullen: Omada-URL, Open API client-id/secret, portalwachtwoord
docker compose up -d --build
```

Bijwerken naar de nieuwste versie uit GitHub:

```sh
docker compose build --pull --no-cache && docker compose up -d
```

Open daarna `http://<host>:8098`, log in en kies onder **Instellingen** de site, het PPSK-profiel
en het gast-VLAN.

Omada: maak onder *Instellingen → Platformintegratie → Open API* een client (client credentials)
met toegang tot de site. Voor type/merk/model en het controleren van switchpoorten is daarnaast
een gewone Omada-gebruiker nodig (`OMADA_WEB_USER` / `OMADA_WEB_PASS`, optioneel).

Runtime-data (`data/`: config, sleutelstatus, database, sessiesleutel) staat buiten git.

## Homey-widget

`homey-widget/` is een Homey Pro-app met een dashboardwidget *Wifi-apparaat aanmelden*:
naam invullen → sleutel + QR-code. Installeren met de Homey CLI:

```sh
cd homey-widget
cp .homeycompose/app.json app.json
homey app install
```

Vul in de widget-instellingen het portal-adres en het API-token (portal → Instellingen) in.
