#!/usr/bin/env python3
"""
test_validator.py — Unit tests for ValidationAgent contracts.
All HTTP mocked via respx. No live network calls.
Claude Code: implement tests following AGENTS.md → ValidationAgent spec.
"""
import pytest
# Claude Code: import ValidationAgent and write tests for:
# - legitimacy score assignment rules (high/medium/low)
# - content-type rejection (text/html stream_url → rejected)
# - robots.txt compliance (domain blocked → candidate skipped)
# - timeout handling (status="unknown", not crash)
