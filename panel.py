"""The owner's dashboard for thinking-of-you: a plugin view (pages/mind/) and the Web API behind it.

AstrBot serves plugin Web APIs only to a logged-in dashboard user (or an API key with the plugin scope). What it shows
is what /tov_log shows; "think once" is /tov_think (a dry run: nothing is sent, nothing is changed). The only change it
can make is letting go of the thoughts she keeps for later.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

PREFIX = "/astrbot_plugin_thinking_of_you/page"


def _ok(data: Any) -> dict:
    return {"status": "ok", "data": data}


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


def _now() -> datetime:
    return datetime.now().astimezone()


def report_rows(report: dict) -> list[dict]:
    """A dry run's thoughts as rows for the page."""
    return [{"kind": kind, "model": model, "decision": final, "reason": reason, "message": message}
            for kind, model, final, reason, message in report.get("thoughts", [])]


class Panel:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.thinking: asyncio.Task | None = None
        self.last_think: dict = {}

    def register(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return False
        routes = [("overview", self.overview, ["GET"]), ("think", self.think, ["POST"]),
                  ("think_status", self.think_status, ["GET"]), ("later_clear", self.later_clear, ["POST"])]
        for name, handler, methods in routes:
            register(f"{PREFIX}/{name}", self._guarded(handler), methods, f"thinking-of-you dashboard: {name}")
        return True

    @staticmethod
    def _guarded(handler):
        async def run(**_):
            try:
                return _ok(await handler())
            except ValueError as exc:
                return _error(str(exc))
            except Exception as exc:  # noqa: BLE001 - the page shows the message instead of a blank screen
                from astrbot.api import logger  # noqa: PLC0415

                logger.warning(f"[thinking-of-you] dashboard {handler.__name__} failed: {exc}")
                return _error(f"{type(exc).__name__}: {exc}")

        run.__name__ = handler.__name__
        return run

    def _state(self):
        from .mind import State  # noqa: PLC0415

        mem = self.plugin._mem()
        if mem is None:
            return None, None
        return mem, State.load(mem.core)

    async def overview(self) -> dict:
        p = self.plugin
        r = p._rules()
        mem, state = await asyncio.to_thread(self._state)
        base = {"enabled": bool(p.cfg.get("enabled", True)), "memory_core": mem is not None,
                "rules": {"daily_cap": r.daily_cap, "quiet_start": r.quiet_start, "quiet_end": r.quiet_end,
                          "min_gap_minutes": r.min_gap_minutes, "max_unanswered": r.max_unanswered,
                          "share_daily_cap": r.share_daily_cap, "tick_minutes": int(p.cfg.get("tick_minutes", 10))},
                "platforms": sorted(p._platforms()),
                "thinking": bool(self.thinking and not self.thinking.done())}
        if mem is None:
            return base
        today = _now().date().isoformat()
        sent_today = state.sent_today if state.day == today else {}
        return {**base,
                "owner": mem.owner_title,
                "knows_where": bool(state.owner_umo),
                "sent_today": sent_today, "sent_today_total": sum(sent_today.values()),
                "unanswered": state.unanswered,
                "last_sent_at": state.last_sent_at, "next_after": state.next_after,
                "thoughts": list(reversed(state.thoughts[-40:])),
                "later": list(state.later),
                "sent": list(reversed(state.sent[-20:])),
                "mistakes": list(state.mistakes)}

    async def think(self) -> dict:
        """Think once now, ignoring the timing rules: what she would think and whether she would speak. Sends
        nothing and changes nothing (a model call: it takes a little while)."""
        if self.thinking and not self.thinking.done():
            raise ValueError("正在想，等一下")
        if self.plugin._mem() is None:
            raise ValueError("没有记忆核心插件，她没法想")
        self.last_think = {"state": "thinking", "started": _now().isoformat()}
        self.thinking = asyncio.create_task(self._think())
        return {"started": True}

    async def _think(self) -> None:
        try:
            report = await self.plugin.think(dry_run=True)
            self.last_think = {"state": "done", "at": _now().isoformat(), "why": report.get("why", ""),
                               "rows": report_rows(report)}
        except Exception as exc:  # noqa: BLE001
            self.last_think = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}

    async def think_status(self) -> dict:
        return dict(self.last_think, thinking=bool(self.thinking and not self.thinking.done()))

    async def later_clear(self) -> dict:
        """Let go of everything she keeps for later (she will not bring it up)."""
        p = self.plugin

        def clear() -> int:
            mem, state = self._state()
            if mem is None:
                raise ValueError("没有记忆核心插件")
            n = len(state.later)
            state.later = []
            state.save(mem.core)
            return n

        async with p._lock:
            n = await asyncio.to_thread(clear)
        return {"cleared": n}
