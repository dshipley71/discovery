#!/usr/bin/env python3
"""
test_feed_validation.py — Unit tests for FeedValidationSkill.
All HTTP mocked via respx.
Claude Code: implement tests following SKILLS.md → FeedValidationSkill spec.
"""
import pytest
# Claude Code: import FeedValidationSkill and write tests for:
# - media content-type → status="live"
# - text/html content-type → rejected (status="dead")
# - 404 / timeout → status="unknown"
# - YouTube nocookie embed URL → exempt from content-type check
