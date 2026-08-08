"""Stable permission values shared by Spiritus policy and approval APIs."""
from __future__ import annotations

from enum import StrEnum


class Access(StrEnum):
    """Policy applied before an agent capability is executed."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    @classmethod
    def parse(cls, value: Access | str) -> Access:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except (AttributeError, ValueError) as exc:
            raise ValueError("access must be 'allow', 'ask', or 'deny'") from exc


class ApprovalDecision(StrEnum):
    """A response to one OpenCode permission request."""

    ONCE = "once"
    ALWAYS = "always"
    REJECT = "reject"

    @classmethod
    def parse(cls, value: ApprovalDecision | str) -> ApprovalDecision:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except (AttributeError, ValueError) as exc:
            raise ValueError("approval decision must be 'once', 'always', or 'reject'") from exc


__all__ = ["Access", "ApprovalDecision"]
