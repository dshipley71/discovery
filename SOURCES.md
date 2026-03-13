# SOURCES.md — Webcam Source Registry

## Overview

This file is the canonical reference for all sources in the public webcam discovery system. It is divided into three sections:

1. **Priority Sources** — Search these first, exhaustively
2. **Blocked Sources** — Never search or include feeds from these
3. **General Discovery** — Everything else; search opportunistically

All agents must consult this file before fetching any source. When a new source is identified by `SourceDiscoverySkill`, it must be added here by a human operator before agents may use it.

---

## Section 1 — Priority Sources (Search First)

### Tier 1: Major Global Webcam Directories

These are high-yield aggregators with structured, city-indexed, open-access listings. Exhaust these before moving to general search.

| Source | URL | Notes | Feed Types |
|--------|-----|-------|-----------|
| **Windy Webcams** | https://www.windy.com/webcams | Largest global database; city-indexed; API-friendly; community-submitted | MJPEG, HLS, iframe |
| **WebcamTaxi** | https://www.webcamtaxi.com | Well-organized by country/city; no auth; direct embed links | iframe, MJPEG |
| **Skyline Webcams** | https://www.skylinewebcams.com | High-quality city panoramas; European focus; free embeds | HLS, iframe |
| **EarthCam** | https://www.earthcam.com | Major landmarks globally; some feeds free without login | HLS, iframe |
| **Roundshot** | https://www.roundshot.com/live | Panoramic city and mountain cams; public embeds | MJPEG, iframe |
| **Panomax** | https://www.panomax.com | Alpine, city, and resort panoramas; free access | MJPEG, iframe |
| **WorldCam** | https://worldcam.eu | Global city and landmark cams; no auth required | iframe, MJPEG |
| **WorldStreamView** | https://worldstreamview.com | Global live stream directory; public access | HLS, iframe |
| **WebCamera24** | https://webcamera24.com | Global webcam directory; city-indexed; no login | iframe, MJPEG |
| **WorldCams.tv** | https://worldcams.tv | Global city, nature, and landmark cams; free access | HLS, iframe |
| **IPLiveCams** | https://iplivecams.com | Global IP camera directory; publicly accessible feeds | MJPEG, HLS |
| **LiveBeaches** | https://livebeaches.com | Global beach and coastal cams; no auth | HLS, iframe |
| **Camscape** | https://camscape.com | Global webcam directory; city and nature cams | iframe, MJPEG |
| **EarthTV** | https://earthtv.com | Global landmark and city cams; high-quality public feeds | HLS, iframe |

---

### Tier 2: Regional & Specialty Directories

High-yield for specific regions or camera types. Search after Tier 1 for relevant geographies.

#### Europe
| Source | URL | Coverage | Notes |
|--------|-----|----------|-------|
| **LiveCameras.gr** | https://www.livecameras.gr | Greece | City, beach, and island cams |
| **Webcam-Galore** | https://www.webcam-galore.com | Europe-wide | Large directory, mixed quality |
| **MeteoBlue Webcams** | https://www.meteoblue.com/webcams | Global/Europe | Weather-context cams, open access |
| **Feratel Webcams** | https://www.feratel.com | Alps/Central Europe | High-quality alpine and resort cams |
| **Webcams.travel** | https://www.webcams.travel | Europe-focused | Tourism-oriented, city-indexed |

#### Oceania
| Source | URL | Coverage | Notes |
|--------|-----|----------|-------|
| **Deckchair** | https://www.deckchair.com | Australia, NZ | Beach, coast, and city cams |
| **Coastalwatch** | https://www.coastalwatch.com | Australia | Surf and coastal cams |
| **BeachCam NZ** | https://www.beachcam.co.nz | New Zealand | Coastal and beach cams |

#### Coastal & Surf
| Source | URL | Coverage | Notes |
|--------|-----|----------|-------|
| **BeachCam** | https://www.beachcam.com | Global coastal | Free tier; surf and beach cams |
| **BeachCam PT** | https://www.beachcam.pt | Portugal/Europe | Portuguese and Spanish coast |
| **Surfline** | https://www.surfline.com/surf-report | Global surf spots | Some cameras free without login; verify per feed |
| **Magicseaweed** | https://www.magicseaweed.com | Global | Surf cams, some free |
| **SurfsideCam** | https://www.surfsidecam.com | US/Global | Beach and surf, publicly accessible |

#### Aviation
| Source | URL | Coverage | Notes |
|--------|-----|----------|-------|
| **Airport Webcams** | https://www.airport-webcams.net | Global airports | Curated airport-specific feeds |
| **LiveATC** (video) | https://www.liveatc.net | Global | Some airports have video feeds alongside audio |
| **Flightradar24 Airport Views** | https://www.flightradar24.com | Global | Some airport live cam embeds |

---

### Tier 3: Government & Infrastructure Sources

Official public feeds from transport authorities, weather agencies, and municipalities. Highly reliable; directly licensed as public.

#### United States
| Source | URL | Notes |
|--------|-----|-------|
| **511 NY** | https://511ny.org | New York state traffic cameras |
| **511 SF Bay** | https://511.org | San Francisco Bay Area traffic |
| **WSDOT** | https://wsdot.wa.gov/travel/traffic/cameras | Washington state DOT cameras |
| **CDOT** | https://cotrip.org | Colorado DOT cameras |
| **TxDOT** | https://txdot.gov | Texas DOT cameras |
| **FDOT SunGuide** | https://fl511.com | Florida DOT cameras |
| **IDOT** | https://gettingaroundillinois.com | Illinois DOT cameras |
| **CALTRANS** | https://cwwp2.dot.ca.gov | California traffic cameras |
| **NOAA / NWS** | https://www.weather.gov | US weather infrastructure cameras |
| **USGS StreamStats** | https://streamstats.usgs.gov | Some stream gauge visual feeds |
| **NPS Webcams** | https://www.nps.gov/subjects/digital/webcams.htm | National Park Service public cams |
| **NASA Public Feeds** | https://www.nasa.gov/multimedia/nasatv | Launch pad and facility cams |

#### United Kingdom
| Source | URL | Notes |
|--------|-----|-------|
| **TfL Road Cameras** | https://tfl.gov.uk/traffic/status | Transport for London; public feeds |
| **Highways England** | https://www.trafficengland.com | National motorway cameras |
| **Traffic Scotland** | https://www.traffic.scot | Scottish road cameras |
| **Traffic Wales** | https://traffic.wales | Welsh road cameras |

#### Scandinavia
| Source | URL | Notes |
|--------|-----|-------|
| **Statens vegvesen** | https://vegvesen.no/trafikk/kart | Norway; open road camera feeds |
| **Trafikverket** | https://www.trafikverket.se/trafikinformation/vag | Sweden; public traffic cameras |
| **Vejdirektoratet** | https://www.vejdirektoratet.dk | Denmark; road cameras |
| **Fintraffic** | https://www.fintraffic.fi | Finland; transport cameras |

#### Other Countries
| Source | URL | Country | Notes |
|--------|-----|---------|-------|
| **Autoroutes France** | https://www.bison-fute.gouv.fr | France | Public road cameras |
| **Autobahn DE** | https://www.autobahn.de | Germany | Federal motorway cameras |
| **RWS** | https://www.rijkswaterstaat.nl | Netherlands | Road and waterway cameras |
| **Highways Austria** | https://www.asfinag.at | Austria | Motorway cameras |
| **INRIX** (public feeds) | varies | Global | Some DOT-partnered feeds are public |

---

### Tier 4: Open Data Portals

City and national open data portals that publish camera feeds or embed links directly.

| Portal | URL | Notes |
|--------|-----|-------|
| **Helsinki Open Data** | https://hri.fi | Finnish capital; some public cam data |
| **Amsterdam Open Data** | https://data.amsterdam.nl | May include public cam metadata |
| **Barcelona Open Data** | https://opendata-ajuntament.barcelona.cat | Catalan municipality open data |
| **NYC Open Data** | https://opendata.cityofnewyork.us | Includes some public feed references |
| **London Datastore** | https://data.london.gov.uk | GLA open data including transport feeds |
| **data.gov** | https://data.gov | US federal open data; some cam references |
| **data.gov.uk** | https://data.gov.uk | UK open data |

---

### Tier 5: Live Streaming Platforms (Public Landmark Streams Only)

Search these for **24/7 public landmark streams only**. Exclude personal broadcasts, events, gaming, and anything requiring login.

| Source | URL | Notes |
|--------|-----|-------|
| **YouTube Live** | https://www.youtube.com/live | Filter: 24/7, no login, public locations |
| **Twitch** (city cams) | https://www.twitch.tv | Niche; some cities have permanent IRL streams |

**Search queries for these platforms:**
```
site:youtube.com/live "[city]" "24/7" live webcam
site:youtube.com "live webcam" "[city]" -"subscribe"
```

---

## Section 2 — Blocked Sources (Never Use)

These sources must never be searched, scraped, or used as a basis for catalog entries. Any camera URL that can only be traced back to a blocked source must be excluded.

### Security Research / Unintended Exposure Tools

| Source | Reason for Block |
|--------|-----------------|
| **Shodan.io** | Discovers devices via port scanning; cameras found here are exposed by misconfiguration, not public by intent. Requires API key. |
| **Censys.io** | Same as Shodan. Security research tool, not a camera directory. |
| **ZoomEye** | Chinese equivalent of Shodan. Same exclusion rationale. |
| **FOFA** | Similar to Shodan. Same exclusion rationale. |
| **Binary Edge** | Network intelligence platform. Same exclusion rationale. |
| **GreyNoise** | Security telemetry. Not a camera source. |
| **Any IP range scanner output** | Cameras found via subnet scanning are not public by design. |

### Private Consumer Security Devices

| Source | Reason for Block |
|--------|-----------------|
| **Nest / Google Home** | Consumer home security; never public by design |
| **Ring** | Residential doorbell/camera system; always private |
| **Arlo** | Consumer home security; always private |
| **Wyze** | Consumer home security; always private |
| **Blink** | Consumer home security; always private |
| **SimpliSafe** | Consumer home security; always private |
| **ADT Pulse** | Consumer security monitoring; always private |

### Enterprise / Commercial CCTV Platforms

| Source | Reason for Block |
|--------|-----------------|
| **Verkada** | Enterprise physical security platform; always auth-gated |
| **Milestone XProtect** | Enterprise VMS; always requires credentials |
| **Genetec** | Enterprise VMS; always requires credentials |
| **Hanwha Wisenet** | Commercial CCTV NVR; auth-gated |
| **Avigilon** | Enterprise CCTV; always private |
| **IPVM** | Industry trade publication; behind paywall |

### Direct Device Access (Misconfigured Private Devices)

| Source | Reason for Block |
|--------|-----------------|
| **Hikvision direct IP feeds** | Manufacturer NVR/DVR; exposed devices are misconfigured, not public |
| **Dahua direct IP feeds** | Same as Hikvision |
| **Axis direct IP feeds** | Same as above — intended for private deployment |
| **Reolink direct feeds** | Consumer/prosumer cameras; public access = misconfiguration |
| **Foscam direct feeds** | Same as above |
| **iSpy / Agent DVR** | Home/office NVR software; cameras are private installations |
| **Any direct IP camera URL** (e.g., `http://[public-ip]:8080`) | Always treat direct-IP camera access as a potential misconfiguration |

### Paywalled / Subscription Services

| Source | Reason for Block |
|--------|-----------------|
| **Patreon-linked streams** | Paid access |
| **OnlyFans** | Entirely out of scope |
| **Substack video** | Subscription content |
| **Any platform requiring payment** | Violates public-only mandate |

### Closed Municipal / Government CCTV

| Source | Reason for Block |
|--------|-----------------|
| **NYC DOT internal CCTV** | Not publicly accessible; different from 511NY |
| **LAPD / police department feeds** | Law enforcement surveillance; not public |
| **UK Police CCTV networks** | Not public |
| **Any internal city CCTV system** | Even if accidentally accessible, exclude |

### Other Blocked Sources

| Source | Reason for Block |
|--------|-----------------|
| **Archive.org (Wayback Machine)** | Archived snapshots only; not live feeds |
| **Private Discord / Slack bots** | Not public web |
| **Any `.m3u8` or RTSP requiring VPN** | Geo/access restricted; not universally public |
| **Insecam.org** | Aggregates misconfigured private cameras; ethically excluded |
| **Opentopia.com** | Same as Insecam — aggregates unintentionally exposed private cameras |
| **Camhacker.com** | Exploits exposed private devices; excluded entirely |
| **Any site that lists "hacked cameras"** | Entirely excluded regardless of phrasing |
| **foto-webcam.eu** | https://www.foto-webcam.eu | image/MJPEG feeds |

---

## Section 3 — General Discovery (Opportunistic)

Any source not on the Priority list or the Block list may be searched opportunistically via `SearchAgent`. All candidates from general discovery must pass through `ValidationAgent` with stricter scrutiny — apply the "Intent Test" from `CLAUDE.md` before including any feed from an unvetted source.

**When a general discovery source yields 5 or more valid cameras**, flag it via `SourceDiscoverySkill` for possible promotion to the Priority list.

---

## Source Registry Maintenance

| Action | Who | Process |
|--------|-----|---------|
| Add new priority source | Human operator | Add to appropriate tier, document feed types and notes |
| Add new blocked source | Human operator | Add with explicit reason for block |
| Promote general → priority | Human operator | After `SourceDiscoverySkill` flags; verify manually first |
| Remove a priority source (goes offline) | Human operator | Move to archive section with date |
| Flag a source for review | Any agent | Add note to source entry, do not auto-remove |

---

## Archived Sources (Previously Active, Now Inactive)

| Source | Last Active | Reason |
|--------|------------|--------|
| *(none yet)* | — | — |
