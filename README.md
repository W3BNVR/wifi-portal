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
- **Gastsleutels met tijdslimiet**: 4 uur, 24 uur, tot maandag 09:00, een week of eigen duur; QR-code en
  printbaar kaartje; verdwijnen vanzelf.
- **Waakhond**: markeer apparaten als kritiek; melding (logboek + Homey) als ze X minuten offline zijn en
  weer terug. Nieuwe apparaten worden binnen ±30 s gemeld.
- **Internetpauze** per apparaat: direct, voor X minuten of op een dagschema (blokkeert de client in Omada).
- **Onderhoud**: zwak signaal (met 24 u/7 d-gemiddelde per apparaat), VLAN-check op basis van eigen regels,
  opruimlijst (lang niet geziene sleutels/apparaten), nachtelijke backup met herstel per onderdeel.
- **ACL tussen VLAN's**: matrix (standaard dicht, jij zet open wat mag), stateful zodat antwoorden terugkomen,
  uitzonderingen per apparaat en desnoods per poort/protocol (IP wordt automatisch vastgezet). Beheert alleen
  regels die met `portal:` beginnen; eigen regels blijven ongemoeid. Snapshot vooraf, versies terugzetten, en
  automatisch terugdraaien als de pagina na toepassen niet binnen 3 minuten bevestigt.
- **API voor Homey** (Bearer-token uit Instellingen): `/api/v1/new`, `/api/v1/key/<naam>`, `/api/v1/events`,
  `/api/v1/devices`, `/api/v1/guest`, `/api/v1/pause`.

## Installeren (Docker)

Er is een kant-en-klaar image, automatisch gebouwd uit deze repo (amd64 en arm64):

```
ghcr.io/w3bnvr/wifi-portal:latest
```

Met Docker Compose: zet `docker-compose.yml` en `.env` in een map en start:

```sh
cp .env.example .env      # invullen: Omada-URL, Open API client-id/secret, portalwachtwoord
docker compose up -d
```

Bijwerken naar de nieuwste versie: `docker compose pull && docker compose up -d`.

**ZimaOS / CasaOS:** importeer [`zimaos-compose.yml`](zimaos-compose.yml) via *App Store → Custom
install → Import* en vul de omgevingsvariabelen in.

In Portainer of Unraid kan het ook zonder compose: image
`ghcr.io/w3bnvr/wifi-portal:latest`, poort `8080` in de container (bv. naar `8098`), een map naar
`/data`, en de omgevingsvariabelen uit `.env.example`.

Liever zelf bouwen uit de broncode? Gebruik in de compose in plaats van `image:`:

```yaml
build:
  context: https://github.com/W3BNVR/wifi-portal.git#main:portal
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

Vul in de widget-instellingen het portal-adres en het API-token (portal → Instellingen) in, en voor de
Flow-kaarten ook in de app-instellingen (Apps → Wifi-portal → Instellingen).

Flow-kaarten:
- *Wanneer*: nieuw apparaat op het netwerk · kritiek apparaat offline · kritiek apparaat weer online
- *Dan*: maak een gastsleutel (geeft netwerk + wachtwoord als tags) · internetpauze aan/uit voor een apparaat
