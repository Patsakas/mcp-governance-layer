"""Pytest configuration: set environment variables before any module imports."""

from __future__ import annotations

import os

# Skip Slack signature verification during tests
os.environ.setdefault("SLACK_SKIP_SIGNATURE_VERIFICATION", "true")
