"""Tests for the decision engine module."""

from __future__ import annotations

import pytest

from app.decision_engine import evaluate


class TestDecisionEngine:
    def test_db_drop_always_requires_human_approval(self):
        """db_drop intent must NEVER be auto-approved."""
        assert evaluate("db_drop", {}) is None
        assert evaluate("db_drop", {"amount": 1}) is None
        assert evaluate("db_drop", None) is None

    def test_refund_below_100_is_auto_approved(self):
        """Refund with amount < 100 must be auto-approved."""
        assert evaluate("refund", {"amount": 99}) == "auto_approved"
        assert evaluate("refund", {"amount": 0}) == "auto_approved"
        assert evaluate("refund", {"amount": 99.99}) == "auto_approved"

    def test_refund_exactly_100_requires_human(self):
        """Refund with amount == 100 must NOT be auto-approved."""
        assert evaluate("refund", {"amount": 100}) is None

    def test_refund_above_100_requires_human(self):
        """Refund with amount > 100 must NOT be auto-approved."""
        assert evaluate("refund", {"amount": 101}) is None
        assert evaluate("refund", {"amount": 500}) is None

    def test_refund_without_amount_requires_human(self):
        """Refund with no amount field must NOT be auto-approved."""
        assert evaluate("refund", {}) is None
        assert evaluate("refund", None) is None

    def test_refund_with_non_numeric_amount_requires_human(self):
        """Refund with a non-numeric amount must NOT be auto-approved."""
        assert evaluate("refund", {"amount": "abc"}) is None

    def test_unknown_intent_requires_human(self):
        """Any intent not explicitly handled must require human approval."""
        assert evaluate("deploy", {}) is None
        assert evaluate("send_email", {"to": "a@b.com"}) is None
        assert evaluate("", {}) is None
