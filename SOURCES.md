# SOURCES.md — Webcam Source Registry

## Overview

This file is the canonical reference for all sources in the public webcam discovery system.
The repository is now **HLS-only**: discovery and validation must target direct `.m3u8`
playlists and ignore every other camera transport or embed type.

1. **Priority Sources** — Search these first, exhaustively
2. **Blocked Sources** — Never search or include feeds from these
3. **General Discovery** — Search the open web for additional public `.m3u8` webcams

All agents must consult this file before fetching any source. When a new source is identified
by `SourceDiscoverySkill`, it must be added here by a human operator before agents may use it.

---

## Section 1 — Priority Sources (Search First)

### Tier 1: Major Global HLS Webcam Directories

| Source | URL | Notes | Feed Types |
|--------|-----|-------|-----------|
| **Windy Webcams** | https://www.windy.com/webcams | Large global directory; only keep cameras that expose direct `.m3u8` playlists | HLS (.m3u8) |
| **Skyline Webcams** | https://www.skylinewebcams.com | High-quality city panoramas; validate only direct HLS playlists | HLS (.m3u8) |
| **EarthCam** | https://www.earthcam.com | Major landmarks globally; retain only feeds with direct `.m3u8` URLs | HLS (.m3u8) |
| **WorldViewStream** | https://worldviewstream.com | Global live stream directory with HLS-friendly pages | HLS (.m3u8) |
| **WorldCams.tv** | https://worldcams.tv | Global city, nature, and landmark cams; probe only `.m3u8` feeds | HLS (.m3u8) |
| **LiveBeaches** | https://livebeaches.com | Beach and coastal cams; catalog only direct HLS outputs | HLS (.m3u8) |
| **EarthTV** | https://earthtv.com | Global landmark and city cams; keep only `.m3u8` playlists | HLS (.m3u8) |
| **GeoWebcams** | https://www.geowebcams.com | Curated directory; candidate pages must resolve to direct HLS | HLS (.m3u8) |
| **CamStreamer** | https://camstreamer.com/live | Public network-camera gallery; retain only direct `.m3u8` feeds | HLS (.m3u8) |

### Tier 2: Regional & Specialty HLS Sources

| Source | URL | Coverage | Notes |
|--------|-----|----------|-------|
| **MeteoBlue Webcams** | https://www.meteoblue.com/webcams | Global/Europe | Weather-context cams; accept only direct HLS playlists |
| **Feratel Webcams** | https://www.feratel.com | Alps/Central Europe | Alpine/resort cams with HLS validation only |
| **Deckchair** | https://www.deckchair.com | Australia, NZ | Beach and city cams; retain only `.m3u8` streams |
| **Coastalwatch** | https://www.coastalwatch.com | Australia | Surf cams; skip any feed that is not direct HLS |
| **Airport Webcams** | https://www.airport-webcams.net | Global airports | Curated airport feeds; accept only direct `.m3u8` outputs |

### Tier 3: Government & Infrastructure Sources

| Source | URL | Region | Notes |
|--------|-----|--------|-------|
| **511 NY** | https://511ny.org | United States | Traffic cameras; catalog only direct `.m3u8` feeds |
| **511 SF Bay** | https://511.org | United States | Traffic cameras; ignore JPEG refresh feeds |
| **WSDOT** | https://wsdot.wa.gov/travel/traffic/cameras | United States | Washington traffic cameras; require direct HLS |
| **TfL Road Cameras** | https://tfl.gov.uk/traffic/status | United Kingdom | Keep only publicly reachable `.m3u8` URLs |
| **Trafikverket** | https://www.trafikverket.se/trafikinformation/vag | Sweden | Public traffic cameras; only HLS is eligible |
| **Fintraffic** | https://www.fintraffic.fi | Finland | Public transport cameras; only direct `.m3u8` playlists |

---

## Section 2 — Blocked Sources

Never crawl or catalog these sources. They are blocked because they are private, surveillance-oriented,
auth-gated, or incompatible with the project's public-only and HLS-only policy.

| Source | URL | Reason |
|--------|-----|--------|
| **Insecam** | https://www.insecam.org | Surveillance-oriented and privacy-invasive |
| **Shodan** | https://www.shodan.io | Device search engine; not a public webcam directory |
| **Censys** | https://search.censys.io | Device discovery platform; not an allowed source |
| **ZoomEye** | https://www.zoomeye.org | Device discovery platform; not an allowed source |
| **Fofa** | https://en.fofa.info | Device discovery platform; not an allowed source |
| **Any auth-gated webcam service** | https://example.com/login | Requires login, registration, or subscription |
| **Any non-HLS-only image network** | https://www.foto-webcam.eu | Image/JPEG-only feed network; outside `.m3u8` scope |

---

## Section 3 — General Discovery

Use targeted search only for publicly accessible HLS webcams.

### Query patterns

- `"live webcam" [city] ".m3u8"`
- `"public webcam" [city] "hls" -login -register -subscribe`
- `inurl:webcam OR inurl:livecam [city] ".m3u8"`
- `[city] "traffic camera" ".m3u8"`
- `[city] municipality webcam hls`
- `[city] 観光 ライブカメラ m3u8`
- `[city] cámara en vivo m3u8`
- `[city] caméra en direct m3u8`
- `[city] öffentliche webcam m3u8`

### Exclusions

- Any feed that resolves to HTML instead of a playlist
- Any feed that requires cookies, login, signup, subscription, or opaque JS-only playback
- Any RTSP, MJPEG, MP4-only, JPEG-refresh, DASH, or YouTube-only source
- Any `.m3u8` URL that is geo-restricted or otherwise ambiguously public
