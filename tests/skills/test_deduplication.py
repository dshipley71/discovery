#!/usr/bin/env python3
"""
test_deduplication.py — Unit tests for DeduplicationSkill.
Claude Code: implement tests following SKILLS.md → DeduplicationSkill spec.
"""
import pytest
# Claude Code: import DeduplicationSkill and write tests for:
# - identical label + city → deduplicated to one record
# - fuzzy match above threshold → deduplicated
# - fuzzy match below threshold → both records kept
# - same id slug → collision handled (one kept, one logged)
