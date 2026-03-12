#!/usr/bin/env python3
"""
test_catalog.py — Unit tests for CatalogAgent contracts.
No live network calls.
Claude Code: implement tests following AGENTS.md → CatalogAgent spec.
"""
import pytest
# Claude Code: import CatalogAgent and write tests for:
# - deduplication (same city+label → only one record kept)
# - slug generation (id is stable, lowercase, no special chars)
# - GeoJSON coordinate order ([longitude, latitude] per RFC 7946)
# - records missing lat/lon → skipped + logged
