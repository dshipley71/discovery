#!/usr/bin/env python3
"""
test_geo_enrichment.py — Unit tests for GeoEnrichmentSkill.
Claude Code: implement tests following SKILLS.md → GeoEnrichmentSkill spec.
"""
import pytest
# Claude Code: import GeoEnrichmentSkill and write tests for:
# - record with lat/lon already set → passed through unchanged
# - record missing lat/lon + valid city → coordinates populated
# - record missing lat/lon + unknown city → record flagged/skipped
