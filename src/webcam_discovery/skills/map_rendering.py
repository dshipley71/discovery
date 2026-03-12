#!/usr/bin/env python3
"""
map_rendering.py — Leaflet.js map generation from camera.geojson.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from pydantic import BaseModel


# ── I/O Models ────────────────────────────────────────────────────────────────

class MapRenderingInput(BaseModel):
    """Input for map rendering skill."""

    geojson_path: Path
    output_path: Path = Path("map.html")


class MapRenderingOutput(BaseModel):
    """Output from map rendering skill."""

    path: str
    camera_count: int


# ── MapRenderingSkill ──────────────────────────────────────────────────────────

class MapRenderingSkill:
    """Generate a self-contained HTML Leaflet.js map from camera.geojson."""

    def run(self, input: MapRenderingInput) -> MapRenderingOutput:
        """
        Read camera.geojson and generate a self-contained HTML map file.

        Args:
            input: MapRenderingInput with geojson_path and output_path.

        Returns:
            MapRenderingOutput with path and camera_count.
        """
        geojson_path = input.geojson_path
        output_path = input.output_path

        camera_count = 0
        if geojson_path.exists():
            try:
                with open(geojson_path, encoding="utf-8") as f:
                    data = json.load(f)
                camera_count = len(data.get("features", []))
            except Exception as exc:
                logger.warning("MapRenderingSkill: could not read {}: {}", geojson_path, exc)
                camera_count = 0

        html = self._generate_html(geojson_path.name)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")

        logger.info("MapRenderingSkill: map.html written to '{}' ({} cameras)", output_path, camera_count)
        return MapRenderingOutput(path=str(output_path), camera_count=camera_count)

    def _generate_html(self, geojson_filename: str) -> str:
        """Generate the full self-contained HTML map."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Public Webcam Map</title>

<!-- Leaflet.js 1.9.x -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>

<!-- Leaflet.markercluster -->
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" crossorigin=""/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" crossorigin=""/>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js" crossorigin=""></script>

<!-- hls.js -->
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>

<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #111; color: #eee; height: 100vh; display: flex; flex-direction: column; }}

#stats-bar {{
  background: #1a1a2e; padding: 8px 16px; display: flex; align-items: center;
  gap: 16px; font-size: 13px; border-bottom: 1px solid #333; flex-shrink: 0;
}}
#stats-bar span {{ white-space: nowrap; }}
.stat-live {{ color: #00e5a0; font-weight: bold; }}
.stat-unknown {{ color: #f5a623; font-weight: bold; }}
.stat-dead {{ color: #e05252; font-weight: bold; }}
.stat-total {{ color: #89b4fa; font-weight: bold; }}

#map-container {{ display: flex; flex: 1; overflow: hidden; }}
#map {{ flex: 1; }}

#filter-panel {{
  width: 240px; background: #1a1a2e; border-left: 1px solid #333;
  padding: 12px; overflow-y: auto; font-size: 13px; display: flex; flex-direction: column; gap: 12px;
}}
#filter-panel h3 {{ color: #89b4fa; font-size: 14px; margin-bottom: 4px; }}

.filter-group label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; padding: 2px 0; }}
.filter-group input[type=checkbox] {{ accent-color: #00e5a0; }}
.filter-group input[type=checkbox].unknown-cb {{ accent-color: #f5a623; }}
.filter-group input[type=checkbox].dead-cb {{ accent-color: #e05252; }}

#search-bar {{
  display: flex; gap: 6px; padding: 8px 16px; background: #1a1a2e;
  border-bottom: 1px solid #333; flex-shrink: 0;
}}
#search-input {{
  flex: 1; background: #252540; border: 1px solid #444; color: #eee;
  border-radius: 4px; padding: 6px 10px; font-size: 13px;
}}
#search-btn {{
  background: #3a3a6a; border: 1px solid #555; color: #eee;
  border-radius: 4px; padding: 6px 12px; cursor: pointer; font-size: 13px;
}}
#search-btn:hover {{ background: #4a4a8a; }}

#file-load {{ display: flex; gap: 6px; align-items: center; }}
#file-input {{ display: none; }}
#load-btn {{
  background: #2a4a2a; border: 1px solid #3a6a3a; color: #00e5a0;
  border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 12px;
  white-space: nowrap;
}}
#load-btn:hover {{ background: #3a5a3a; }}
#loaded-file {{ font-size: 11px; color: #888; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 120px; }}

/* Modal */
#modal-overlay {{
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.8);
  z-index: 9999; align-items: center; justify-content: center;
}}
#modal-overlay.open {{ display: flex; }}
#modal {{
  background: #1a1a2e; border: 1px solid #444; border-radius: 8px;
  width: 90vw; max-width: 900px; max-height: 90vh; overflow-y: auto;
  padding: 20px; position: relative;
}}
#modal-close {{
  position: absolute; top: 12px; right: 12px; background: none; border: none;
  color: #aaa; font-size: 20px; cursor: pointer; line-height: 1;
}}
#modal-close:hover {{ color: #fff; }}
#modal-title {{ font-size: 18px; font-weight: bold; color: #89b4fa; margin-bottom: 4px; }}
#modal-location {{ font-size: 13px; color: #aaa; margin-bottom: 12px; }}
#modal-player {{ margin-bottom: 16px; }}
#modal-player video, #modal-player img, #modal-player iframe {{
  width: 100%; max-height: 480px; border-radius: 4px; border: none; background: #000;
}}
#modal-meta {{ font-size: 12px; color: #aaa; line-height: 1.8; }}
#modal-meta strong {{ color: #ccc; }}
#modal-actions {{ display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }}
.modal-btn {{
  background: #252540; border: 1px solid #555; color: #eee; border-radius: 4px;
  padding: 6px 14px; cursor: pointer; font-size: 12px; text-decoration: none;
}}
.modal-btn:hover {{ background: #3a3a6a; }}

/* Empty state */
#empty-overlay {{
  display: none; position: absolute; inset: 0; background: rgba(17,17,17,0.9);
  z-index: 1000; align-items: center; justify-content: center; flex-direction: column; gap: 16px;
}}
#empty-overlay.show {{ display: flex; }}
#empty-overlay p {{ color: #aaa; font-size: 16px; }}

/* Marker icons */
.cam-marker {{ border: none; background: transparent; }}
</style>
</head>
<body>

<div id="stats-bar">
  <strong>🌍 Public Webcam Map</strong>
  <span>Total: <span class="stat-total" id="stat-total">0</span></span>
  <span>Live: <span class="stat-live" id="stat-live">0</span></span>
  <span>Unknown: <span class="stat-unknown" id="stat-unknown">0</span></span>
  <span>Dead: <span class="stat-dead" id="stat-dead">0</span></span>
  <span style="color:#777" id="stat-updated"></span>
  <div id="file-load" style="margin-left:auto">
    <span id="loaded-file">camera.geojson</span>
    <button id="load-btn" onclick="document.getElementById('file-input').click()">📂 Load GeoJSON</button>
    <input type="file" id="file-input" accept=".geojson,.json">
  </div>
</div>

<div id="search-bar">
  <input id="search-input" type="text" placeholder="Search by city or camera label…" />
  <button id="search-btn">Search</button>
</div>

<div id="map-container">
  <div id="map">
    <div id="empty-overlay">
      <p>No camera data loaded.</p>
      <p style="font-size:13px;color:#666">Click 📂 Load GeoJSON above or place camera.geojson alongside map.html</p>
    </div>
  </div>
  <div id="filter-panel">
    <div>
      <h3>Status</h3>
      <div class="filter-group">
        <label><input type="checkbox" class="status-cb" value="live" checked> <span style="color:#00e5a0">● Live</span></label>
        <label><input type="checkbox" class="status-cb unknown-cb" value="unknown" checked> <span style="color:#f5a623">● Unknown</span></label>
        <label><input type="checkbox" class="status-cb dead-cb" value="dead"> <span style="color:#e05252">● Dead</span></label>
      </div>
    </div>
    <div>
      <h3>Legitimacy</h3>
      <div class="filter-group">
        <label><input type="checkbox" class="legit-cb" value="high" checked> High</label>
        <label><input type="checkbox" class="legit-cb" value="medium" checked> Medium</label>
      </div>
    </div>
    <div>
      <h3>Feed Type</h3>
      <div class="filter-group" id="feed-type-filters"></div>
    </div>
    <div>
      <h3>Continent</h3>
      <select id="continent-select" style="width:100%;background:#252540;color:#eee;border:1px solid #444;padding:4px;border-radius:4px;font-size:12px">
        <option value="">All Continents</option>
      </select>
    </div>
  </div>
</div>

<!-- Modal -->
<div id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div id="modal">
    <button id="modal-close" onclick="closeModal()">✕</button>
    <div id="modal-title"></div>
    <div id="modal-location"></div>
    <div id="modal-player"></div>
    <div id="modal-meta"></div>
    <div id="modal-actions"></div>
  </div>
</div>

<script>
// ── Map setup ──────────────────────────────────────────────────────────────────
const map = L.map('map').setView([20, 0], 2);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '© <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 20,
}}).addTo(map);

const clusterGroup = L.markerClusterGroup({{ chunkedLoading: true }});
map.addLayer(clusterGroup);

let allFeatures = [];
const STATUS_COLORS = {{ live: '#00e5a0', unknown: '#f5a623', dead: '#e05252' }};

function makeSvgIcon(status) {{
  const color = STATUS_COLORS[status] || '#888';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="32" viewBox="0 0 24 32">
    <path d="M12 0C5.4 0 0 5.4 0 12c0 9 12 20 12 20S24 21 24 12C24 5.4 18.6 0 12 0z" fill="${{color}}" opacity="0.9"/>
    <circle cx="12" cy="12" r="5" fill="white" opacity="0.6"/>
  </svg>`;
  return L.divIcon({{
    html: svg, className: 'cam-marker',
    iconSize: [24, 32], iconAnchor: [12, 32], popupAnchor: [0, -32]
  }});
}}

function updateStats(features) {{
  const live = features.filter(f => f.properties.status === 'live').length;
  const unknown = features.filter(f => f.properties.status === 'unknown').length;
  const dead = features.filter(f => f.properties.status === 'dead').length;
  document.getElementById('stat-total').textContent = features.length;
  document.getElementById('stat-live').textContent = live;
  document.getElementById('stat-unknown').textContent = unknown;
  document.getElementById('stat-dead').textContent = dead;
}}

function loadGeoJSON(data, filename) {{
  allFeatures = data.features || [];
  document.getElementById('loaded-file').textContent = filename || 'data';
  if (data.metadata && data.metadata.generated) {{
    document.getElementById('stat-updated').textContent = 'Updated: ' + data.metadata.generated.substring(0, 10);
  }}
  populateFilters(allFeatures);
  applyFilters();
  document.getElementById('empty-overlay').classList.toggle('show', allFeatures.length === 0);
}}

function populateFilters(features) {{
  const feedTypes = [...new Set(features.map(f => f.properties.feed_type).filter(Boolean))].sort();
  const continents = [...new Set(features.map(f => f.properties.continent).filter(Boolean))].sort();
  const ftDiv = document.getElementById('feed-type-filters');
  ftDiv.innerHTML = feedTypes.map(ft =>
    `<label><input type="checkbox" class="feed-cb" value="${{ft}}" checked> ${{ft}}</label>`
  ).join('');
  const contSel = document.getElementById('continent-select');
  continents.forEach(c => {{
    const opt = document.createElement('option');
    opt.value = c; opt.textContent = c;
    contSel.appendChild(opt);
  }});
  document.querySelectorAll('.feed-cb, .status-cb, .legit-cb').forEach(cb => cb.addEventListener('change', applyFilters));
  contSel.addEventListener('change', applyFilters);
}}

function applyFilters() {{
  const activeStatuses = new Set([...document.querySelectorAll('.status-cb:checked')].map(cb => cb.value));
  const activeLegit = new Set([...document.querySelectorAll('.legit-cb:checked')].map(cb => cb.value));
  const activeFeedTypes = new Set([...document.querySelectorAll('.feed-cb:checked')].map(cb => cb.value));
  const activeCont = document.getElementById('continent-select').value;
  const filtered = allFeatures.filter(f => {{
    const p = f.properties;
    if (!activeStatuses.has(p.status)) return false;
    if (!activeLegit.has(p.legitimacy_score)) return false;
    if (activeFeedTypes.size > 0 && !activeFeedTypes.has(p.feed_type)) return false;
    if (activeCont && p.continent !== activeCont) return false;
    return true;
  }});
  clusterGroup.clearLayers();
  filtered.forEach(f => {{
    const coords = f.geometry.coordinates;
    const p = f.properties;
    const marker = L.marker([coords[1], coords[0]], {{ icon: makeSvgIcon(p.status) }});
    marker.bindTooltip(`<strong>${{p.label}}</strong><br>${{p.city}}, ${{p.country}}<br><span style="color:${{STATUS_COLORS[p.status]}}">${{p.status}}</span>`, {{ sticky: true }});
    marker.on('click', () => openModal(p));
    clusterGroup.addLayer(marker);
  }});
  updateStats(filtered);
}}

// ── Search ─────────────────────────────────────────────────────────────────────
document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', e => {{ if(e.key==='Enter') doSearch(); }});

function doSearch() {{
  const q = document.getElementById('search-input').value.trim().toLowerCase();
  if (!q) return;
  const match = allFeatures.find(f => {{
    const p = f.properties;
    return (p.city || '').toLowerCase().includes(q) || (p.label || '').toLowerCase().includes(q);
  }});
  if (match) {{
    const [lon, lat] = match.geometry.coordinates;
    map.flyTo([lat, lon], 13);
  }}
}}

// ── Modal ──────────────────────────────────────────────────────────────────────
function openModal(p) {{
  document.getElementById('modal-title').textContent = p.label || 'Camera';
  document.getElementById('modal-location').textContent =
    [p.city, p.region, p.country, p.continent].filter(Boolean).join(', ');

  const playerDiv = document.getElementById('modal-player');
  playerDiv.innerHTML = '';
  if (p.direct_stream_url && p.feed_type === 'HLS') {{
    const video = document.createElement('video');
    video.controls = true; video.autoplay = false; video.muted = true;
    if (Hls.isSupported()) {{
      const hls = new Hls(); hls.loadSource(p.direct_stream_url); hls.attachMedia(video);
    }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
      video.src = p.direct_stream_url;
    }}
    playerDiv.appendChild(video);
  }} else if (p.direct_stream_url && p.feed_type === 'MJPEG') {{
    const img = document.createElement('img');
    img.src = p.direct_stream_url; img.alt = p.label || 'stream';
    playerDiv.appendChild(img);
  }} else if (p.stream_url || p.url) {{
    const iframe = document.createElement('iframe');
    iframe.src = p.stream_url || p.url;
    iframe.allow = 'autoplay; fullscreen';
    iframe.style.height = '400px';
    playerDiv.appendChild(iframe);
  }} else {{
    playerDiv.innerHTML = '<p style="color:#888;padding:20px">No stream URL available.</p>';
  }}

  const yesNo = v => v ? 'Yes' : 'No';
  document.getElementById('modal-meta').innerHTML = `
    <strong>Feed Type:</strong> ${{p.feed_type || '—'}}<br>
    <strong>Source:</strong> ${{p.source_directory || '—'}}<br>
    <strong>Legitimacy:</strong> ${{p.legitimacy_score || '—'}}<br>
    <strong>Requires JS:</strong> ${{yesNo(p.requires_js)}}<br>
    <strong>Geo Restricted:</strong> ${{yesNo(p.geo_restricted)}}<br>
    <strong>Last Verified:</strong> ${{p.last_verified || '—'}}<br>
    ${{p.notes ? `<strong>Notes:</strong> ${{p.notes}}<br>` : ''}}
  `;

  const actionsDiv = document.getElementById('modal-actions');
  actionsDiv.innerHTML = '';
  if (p.url) {{
    const a = document.createElement('a');
    a.href = p.url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = '🔗 Open source page';
    a.className = 'modal-btn'; actionsDiv.appendChild(a);
  }}
  (p.source_refs || []).forEach((ref, i) => {{
    const a = document.createElement('a');
    a.href = ref; a.target = '_blank'; a.rel = 'noopener'; a.textContent = `Source ${{i+1}}`;
    a.className = 'modal-btn'; actionsDiv.appendChild(a);
  }});

  document.getElementById('modal-overlay').classList.add('open');
}}

function closeModal() {{
  document.getElementById('modal-overlay').classList.remove('open');
  document.getElementById('modal-player').innerHTML = '';
}}

document.addEventListener('keydown', e => {{ if(e.key === 'Escape') closeModal(); }});

// ── File picker ────────────────────────────────────────────────────────────────
document.getElementById('file-input').addEventListener('change', e => {{
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = ev => {{
    try {{
      const data = JSON.parse(ev.target.result);
      loadGeoJSON(data, file.name);
    }} catch(err) {{
      alert('Invalid GeoJSON file: ' + err.message);
    }}
  }};
  reader.readAsText(file);
}});

// ── Auto-load camera.geojson ───────────────────────────────────────────────────
fetch('{geojson_filename}')
  .then(r => {{ if(!r.ok) throw new Error('not found'); return r.json(); }})
  .then(data => loadGeoJSON(data, '{geojson_filename}'))
  .catch(() => {{
    document.getElementById('empty-overlay').classList.add('show');
    updateStats([]);
  }});
</script>
</body>
</html>
"""


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    geojson = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("camera.geojson")
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("map.html")
    skill = MapRenderingSkill()
    result = skill.run(MapRenderingInput(geojson_path=geojson, output_path=output))
    logger.info("Generated {} ({} cameras)", result.path, result.camera_count)
