"""Alarm board: raise, clear and rank section alarms."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig
from ..core.ids import validate_token
from ..errors import NotFoundError, ValidationError
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore

SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES, start=1)}


class AlarmBoard:
    """Keeps a capped history of every transition and folds it into active alarms.

    Repeating an alarm that is still active merges into the existing entry
    (keyed by alarm code) and increments its occurrence count instead of
    adding another row to the board.
    """

    document = "alarm-board"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.audit = audit
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "history": self._history[-self.config.alarm.history_limit :],
            },
        )

    def _active_map(self) -> dict[str, dict[str, Any]]:
        """Fold the transition history into one entry per active alarm code."""
        active: dict[str, dict[str, Any]] = {}
        for event in self._history:
            code = event["code"]
            if event.get("cleared_at") is not None:
                active.pop(code, None)
                continue
            existing = active.get(code)
            if existing is None:
                alarm = dict(event)
                alarm["occurrences"] = 1
                active[code] = alarm
            else:
                existing["occurrences"] += 1
                existing["raised_at"] = event["raised_at"]
                if event.get("reason"):
                    existing["reason"] = event["reason"]
        return active

    def raise_alarm(
        self,
        code: str,
        *,
        severity: str,
        message: str,
        target: str = "line",
        reason: str = "",
    ) -> dict[str, Any]:
        label = validate_token(code, field_name="alarm code")
        if severity not in SEVERITIES:
            raise ValidationError("unknown alarm severity", severity=severity, permitted=list(SEVERITIES))
        active = self._active_map()
        existing = active.get(label)
        if existing is not None:
            occurrences = int(existing["occurrences"]) + 1
            alarm = {
                "code": label,
                "severity": existing["severity"],
                "message": existing["message"],
                "target": existing["target"],
                "reason": str(reason) or existing["reason"],
                "raised_at": self.clock.timestamp(),
                "cleared_at": None,
                "occurrences": occurrences,
            }
        else:
            alarm = {
                "code": label,
                "severity": severity,
                "message": str(message),
                "target": str(target),
                "reason": str(reason),
                "raised_at": self.clock.timestamp(),
                "cleared_at": None,
                "occurrences": 1,
            }
        self._history.append(dict(alarm))
        self.persist()
        self.audit.record("alarm-raise", label, f"{severity}: {message}", cause=None)
        return dict(alarm)

    def clear(self, code: str, *, reason: str) -> dict[str, Any]:
        label = str(code)
        active = self._active_map()
        existing = active.get(label)
        if existing is None:
            raise NotFoundError("alarm is not active", code=label)
        alarm = dict(existing)
        alarm["cleared_at"] = self.clock.timestamp()
        alarm["clear_reason"] = str(reason)
        self._history.append(dict(alarm))
        self.persist()
        self.audit.record("alarm-clear", label, str(reason), cause=None)
        return dict(alarm)

    def active(self, *, severity: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        selected = list(self._active_map().values())
        if severity is not None:
            selected = [alarm for alarm in selected if alarm["severity"] == severity]
        if target is not None:
            selected = [alarm for alarm in selected if alarm["target"] == str(target)]
        return [dict(alarm) for alarm in selected]

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def highest_severity(self) -> str | None:
        highest: str | None = None
        for alarm in self._active_map().values():
            if highest is None or SEVERITY_RANK[alarm["severity"]] > SEVERITY_RANK[highest]:
                highest = str(alarm["severity"])
        return highest

    def counts(self) -> dict[str, Any]:
        active = list(self._active_map().values())
        by_severity = {name: 0 for name in SEVERITIES}
        for alarm in active:
            by_severity[alarm["severity"]] += 1
        total_raises = sum(1 for event in self._history if event.get("cleared_at") is None)
        return {
            "active": len(active),
            "total": total_raises,
            "by_severity": by_severity,
            "highest": self.highest_severity(),
        }

__all__ = ["SEVERITIES", "SEVERITY_RANK", "AlarmBoard"]
