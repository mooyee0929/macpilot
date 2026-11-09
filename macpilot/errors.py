"""Structured failures for the ref-based (headless) tools.

Every failure carries a code the calling agent can branch on and a delivery disposition that
says whether anything reached the app. Only ``not_delivered`` is safe to retry as-is.
"""

from __future__ import annotations

from typing import Literal

Delivery = Literal["not_delivered", "delivered_unverified", "uncertain"]

INVALID_ARGS = "INVALID_ARGS"
STALE_REF = "STALE_REF"
ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
ACTION_FAILED = "ACTION_FAILED"
POLICY_DENIED = "POLICY_DENIED"
APP_CLAIMED = "APP_CLAIMED"
APP_NOT_FOUND = "APP_NOT_FOUND"
APP_UNRESPONSIVE = "APP_UNRESPONSIVE"
PERM_DENIED = "PERM_DENIED"
TIMEOUT = "TIMEOUT"


class DeskError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        delivery: Delivery = "not_delivered",
        hint: str = "",
    ) -> None:
        self.code = code
        self.message = message
        self.delivery: Delivery = delivery
        self.hint = hint
        text = f"{code}: {message} [{delivery}]"
        if hint:
            text += f" Next: {hint}"
        super().__init__(text)
