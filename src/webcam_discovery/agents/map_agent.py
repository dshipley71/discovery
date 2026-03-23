"""
map_agent.py — MapAgent
=======================
Generates map.html from the canonical Leaflet.js template
(src/webcam_discovery/templates/map_template.html).

Responsibilities
----------------
* Copy the versioned template to the output path as ``map.html``.
* Verify that ``camera.geojson`` exists alongside the output so the map
  can auto-load on page open.
* Log a reproducible summary of what was written and how to open the map.

The HTML template is fully self-contained:
  - Auto-loads ``camera.geojson`` when served over HTTP(S).
  - Shows an empty-state overlay (drag-and-drop + file picker) when opened
    directly from disk via ``file://``.
  - All filtering, table view, clustering, heatmap, and popup logic lives
    entirely inside the HTML file — no server-side rendering required.

Template location
-----------------
  src/webcam_discovery/templates/map_template.html

Output
------
  <output_dir>/map.html     — the interactive Leaflet map
  <output_dir>/camera.geojson  — must be written by CatalogAgent first
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATE_REL = Path(__file__).parent / "templates" / "map_template.html"
"""Path to the bundled Leaflet map template, relative to this module."""

_OUTPUT_FILENAME = "map.html"
_GEOJSON_FILENAME = "camera.geojson"


# ---------------------------------------------------------------------------
# MapAgent
# ---------------------------------------------------------------------------

class MapAgent:
    """Generate ``map.html`` from the versioned Leaflet template.

    Parameters
    ----------
    output_dir:
        Directory where ``map.html`` will be written.  Must also contain
        (or will contain) ``camera.geojson`` produced by ``CatalogAgent``.
    template_path:
        Override the default template location.  Useful for tests or if the
        template is managed outside the package.
    """

    def __init__(
        self,
        output_dir: str | Path = ".",
        template_path: str | Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.template_path = (
            Path(template_path).resolve()
            if template_path is not None
            else _TEMPLATE_REL.resolve()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> Path:
        """Write ``map.html`` to :attr:`output_dir`.

        Returns
        -------
        Path
            Absolute path to the written ``map.html``.

        Raises
        ------
        FileNotFoundError
            If the template file cannot be found.
        """
        self._validate_template()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        dest = self.output_dir / _OUTPUT_FILENAME
        shutil.copy2(self.template_path, dest)

        logger.info(f"[MapAgent] Wrote {dest} ({dest.stat().st_size:,} bytes)")
        self._warn_if_geojson_missing()
        self._log_open_instructions(dest)

        return dest

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_template(self) -> None:
        if not self.template_path.is_file():
            raise FileNotFoundError(
                f"[MapAgent] Map template not found: {self.template_path}\n"
                f"Expected location: src/webcam_discovery/templates/map_template.html"
            )
        logger.debug(f"[MapAgent] Using template: {self.template_path}")

    def _warn_if_geojson_missing(self) -> None:
        geojson = self.output_dir / _GEOJSON_FILENAME
        if not geojson.is_file():
            logger.warning(
                f"[MapAgent] {_GEOJSON_FILENAME} not found in {self.output_dir}. "
                "Run CatalogAgent first to generate camera data, or the map will "
                "show an empty-state overlay until a GeoJSON file is loaded manually."
            )
        else:
            logger.info(
                f"[MapAgent] Found {geojson.name} "
                f"({geojson.stat().st_size:,} bytes) — map will auto-load on HTTP serve."
            )

    @staticmethod
    def _log_open_instructions(map_path: Path) -> None:
        parent = map_path.parent
        logger.info(
            "[MapAgent] Map ready.  To view with auto-load enabled:\n"
            f"    cd {parent}\n"
            "    python3 -m http.server 8000\n"
            "    # then open http://localhost:8000/map.html\n"
            "\n"
            "    Alternatively, open map.html directly in a browser and use the\n"
            "    drag-and-drop zone or 📂 Load GeoJSON button to load camera data."
        )


# ---------------------------------------------------------------------------
# Standalone entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI convenience wrapper: ``python -m webcam_discovery.agents.map_agent``."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate map.html from the canonical Leaflet template."
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write map.html into (default: current directory).",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Override the bundled template path.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level)
    agent = MapAgent(output_dir=args.output_dir, template_path=args.template)
    out = agent.run()
    print(f"map.html written to: {out}")


if __name__ == "__main__":
    main()
