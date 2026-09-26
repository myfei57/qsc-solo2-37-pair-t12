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

_RAISE_FIELDS = ("code", "severity", "message", "target", "reason", "raised_at")


class AlarmBoard:
    """Keeps the active alarms and a capped history of every transition.

    Active alarms are merged by code: repeating an active code updates the row
    and bumps its ``occurrences`` counter instead of hanging another entry on
    the board. The board's highest severity is ranked by ``SEVERITY_RANK``,
    never by arrival order.
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
        self._active: dict[str, dict[str, Any]] = {}
        self._total_raises = 0
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        payload = stored.payload
        self._history = [dict(item) for item in payload.get("history", [])]
        snapshot = payload.get("active")
        if isinstance(snapshot, list):
            self._active = {str(item["code"]): dict(item) for item in snapshot if "code" in item}
        else:
            self._active = self._replay(self._history)
        total = payload.get("total_raises")
        self._total_raises = (
            int(total)
            if isinstance(total, int)
            else sum(1 for item in self._history if item.get("cleared_at") is None)
        )

    @staticmethod
    def _replay(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Derive the active rows from a raise/clear transition stream."""

        active: dict[str, dict[str, Any]] = {}
        for item in history:
            code = str(item["code"])
            if item.get("cleared_at") is not None:
                active.pop(code, None)
            else:
                active[code] = AlarmBoard._merge_raise(active.get(code), item)
        return active

    @staticmethod
    def _merge_raise(existing: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
        if existing is None:
            alarm = {field: event[field] for field in _RAISE_FIELDS}
            alarm["first_raised_at"] = event["raised_at"]
            alarm["occurrences"] = 1
            return alarm
        alarm = {field: event[field] for field in _RAISE_FIELDS}
        alarm["first_raised_at"] = existing["first_raised_at"]
        alarm["occurrences"] = int(existing["occurrences"]) + 1
        return alarm

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "history": self._history[-self.config.alarm.history_limit :],
                "active": list(self._active.values()),
                "total_raises": self._total_raises,
            },
        )

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
        event = {
            "code": label,
            "severity": severity,
            "message": str(message),
            "target": str(target),
            "reason": str(reason),
            "raised_at": self.clock.timestamp(),
            "cleared_at": None,
        }
        merged = self._merge_raise(self._active.get(label), event)
        self._active[label] = merged
        self._total_raises += 1
        self._history.append(dict(event))
        self.persist()
        self.audit.record(
            "alarm-raise",
            label,
            f"{severity}: {message} (occurrence {merged['occurrences']})",
            cause=None,
        )
        return dict(merged)

    def clear(self, code: str, *, reason: str) -> dict[str, Any]:
        label = str(code)
        active = self._active.pop(label, None)
        if active is None:
            raise NotFoundError("alarm is not active", code=label)
        event = {field: active[field] for field in _RAISE_FIELDS}
        event["cleared_at"] = self.clock.timestamp()
        event["clear_reason"] = str(reason)
        self._history.append(dict(event))
        self.persist()
        self.audit.record("alarm-clear", label, str(reason), cause=None)
        return dict(event)

    def active(self, *, severity: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        selected = list(self._active.values())
        if severity is not None:
            selected = [alarm for alarm in selected if alarm["severity"] == severity]
        if target is not None:
            selected = [alarm for alarm in selected if alarm["target"] == str(target)]
        ranked = sorted(
            selected,
            key=lambda alarm: (-SEVERITY_RANK[alarm["severity"]], alarm["first_raised_at"], alarm["code"]),
        )
        return [dict(alarm) for alarm in ranked]

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def highest_severity(self) -> str | None:
        if not self._active:
            return None
        return max(self._active.values(), key=lambda alarm: SEVERITY_RANK[alarm["severity"]])["severity"]

    def counts(self) -> dict[str, Any]:
        by_severity = {name: 0 for name in SEVERITIES}
        for alarm in self._active.values():
            by_severity[alarm["severity"]] += 1
        return {
            "active": len(self._active),
            "total": self._total_raises,
            "by_severity": by_severity,
            "highest": self.highest_severity(),
        }

__all__ = ["SEVERITIES", "SEVERITY_RANK", "AlarmBoard"]
