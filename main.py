"""thinking-of-you: she reaches out on her own when something is really on her mind.

Every `tick_minutes`: hard rules (only the owner, private chat, quiet hours, daily cap, gaps, stop after
unanswered messages), then thoughts of four kinds (a mistake, the owner's plan whose day has come, something she
saw online, missing them), then one model call deciding for each: now / later / no. At most one message goes out.
Kept thoughts are offered to her when the owner comes back. Needs the memory-core plugin.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest

from .mind import (
    Rules, State, apply_rules, build_user, followup_candidates, gate, later_block, longing_candidate,
    mistake_candidates, number, owner_hour_weight, parse, share_candidates, SYSTEM,
)

MEMORY_CORE = "astrbot_plugin_memory_core"
# AstrBot gives the model these on its own; they would let her message or schedule outside the rules
BUILTIN_PROACTIVE_TOOLS = ("send_message_to_user", "future_task")


def _now() -> datetime:
    return datetime.now().astimezone()


class ThinkingOfYou(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg = config or {}
        self.rng = random.Random()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.cfg.get("enabled", True):
            self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------------------------------------------------------------- memory-core

    def _mem(self):
        try:
            meta = self.context.get_registered_star(MEMORY_CORE)
        except Exception:  # noqa: BLE001
            return None
        plugin = getattr(meta, "star_cls", None) if meta and getattr(meta, "activated", True) else None
        return plugin if getattr(plugin, "core", None) is not None else None

    def _rules(self) -> Rules:
        c = self.cfg
        return Rules(quiet_start=int(c.get("quiet_start", 23)), quiet_end=int(c.get("quiet_end", 9)),
                     daily_cap=int(c.get("daily_cap", 3)), min_gap_minutes=int(c.get("min_gap_minutes", 120)),
                     max_unanswered=int(c.get("max_unanswered", 2)),
                     share_daily_cap=int(c.get("share_daily_cap", 1)),
                     longing_chance=float(c.get("longing_chance", 0.3)))

    def _platforms(self) -> set[str]:
        return {str(p).strip() for p in self.cfg.get("platforms", ["webchat"]) if str(p).strip()}

    def _owner_private(self, mem, event: AstrMessageEvent) -> bool:
        return not event.get_group_id() and str(event.get_sender_id()) in set(mem.owner_ids)

    # ---------------------------------------------------------------- hooks

    @filter.event_message_type(filter.EventMessageType.ALL, priority=4)
    async def on_message(self, event: AstrMessageEvent):
        """The owner said something: remember where to reach them; what she sent counts as answered."""
        mem = self._mem()
        if mem is None or not self._owner_private(mem, event):
            return
        if (event.get_platform_name() or "") not in self._platforms():
            return
        space, _, _ = mem._where(event)

        def write():
            state = State.load(mem.core)
            state.owner_umo, state.owner_space = event.unified_msg_origin, space
            state.owner_spoke(_now())
            state.save(mem.core)
        await asyncio.to_thread(write)

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """Everywhere: AstrBot's own send-message and scheduling tools are taken away, so every message she
        starts goes through this plugin's rules (and into her memory). In the owner's private chat: what she kept
        to tell them, and that they just answered her."""
        if self.cfg.get("block_builtin_proactive", True) and req.func_tool is not None:
            for name in BUILTIN_PROACTIVE_TOOLS:
                req.func_tool.remove_tool(name)
        mem = self._mem()
        if mem is None or not self._owner_private(mem, event):
            return

        def block():
            state = State.load(mem.core)
            text = later_block(state, _now(), mem.owner_title, self._rules())
            state.save(mem.core)
            return text
        text = await asyncio.to_thread(block)
        if text:
            req.system_prompt = f"{(req.system_prompt or '').rstrip()}\n\n{text}\n"

    # ---------------------------------------------------------------- thinking

    def _jitter(self) -> float:
        return max(0.0, min(0.9, float(self.cfg.get("timing_jitter", 0.2))))

    async def _loop(self) -> None:
        from memory_core.now import jitter

        await asyncio.sleep(60)
        while True:
            try:
                await self.think()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[thinking-of-you] tick failed: {exc}")
            await asyncio.sleep(jitter(max(1, int(self.cfg.get("tick_minutes", 10))) * 60, self._jitter(), self.rng))

    async def think(self, *, dry_run: bool = False) -> dict:
        """One tick. dry_run: ignore timing rules, decide, but send nothing and change no state."""
        mem = self._mem()
        if mem is None:
            return {"why": "没有 memory-core 插件"}
        provider_id = mem._provider_id()
        if not provider_id:
            return {"why": "没有可用的聊天模型"}
        async with self._lock:
            return await self._think(mem, provider_id, dry_run)

    async def _think(self, mem, provider_id: str, dry_run: bool) -> dict:
        from memory_core import Audience
        from memory_core.now import _owner_messages, ago, describe, jitter

        core, rules, now = mem.core, self._rules(), _now()
        state = await asyncio.to_thread(State.load, core)
        state.roll_day(now.date())
        owner_spaces = mem._owner_spaces()
        owner_times = await asyncio.to_thread(_owner_messages, core, owner_spaces, 400)
        owner_last = owner_times[0] if owner_times else None
        snap = await asyncio.to_thread(mem._snapshot)
        ok, must_only, why = gate(state, now, owner_last, rules, asleep=getattr(snap, "asleep", None),
                                  busy_minutes=jitter(rules.owner_busy_minutes, self._jitter(), self.rng))
        if dry_run:
            ok, must_only = True, False
        if not ok:
            return {"why": why}

        cands = mistake_candidates(state)
        watermark = state.considered_until
        if not must_only:
            plans = await asyncio.to_thread(self._dated_owner_memories, core)
            cands += followup_candidates(plans, state, now)
            shares, watermark = await asyncio.to_thread(share_candidates, core, state)
            cands += shares
            if not self._declined_longing_recently(state, now):
                gap = ago(now - owner_last, owner_last) if owner_last else ""
                c = longing_candidate(state, snap.longing, owner_last, now.hour,
                                      owner_hour_weight(owner_times, now.hour), rules, self.rng, gap)
                if c is not None:
                    cands.append(c)
        if not cands:
            if not dry_run and watermark != state.considered_until:
                state.considered_until = watermark
                await asyncio.to_thread(state.save, core)
            return {"why": why or "心里没什么特别想说的"}
        number(cands)

        owner = mem.owner_title
        space = getattr(state, "owner_space", "") or (owner_spaces[0] if owner_spaces else "")
        interests = []
        if space:
            hits = await asyncio.to_thread(core.recall, f"{owner}喜欢 关心 兴趣 游戏 最近在忙",
                                           Audience(space, owner_present=True), 8, reinforce=False)
            interests = [h.memory.text for h in hits]
        system = SYSTEM.format(persona=mem._persona("self", "self-image", "voice"),
                               now_note=describe(snap, owner_present=True, owner_title=owner), owner=owner)
        recent_chat = await asyncio.to_thread(self._recent_chat, core, space, owner, now)
        resp = await self.context.llm_generate(chat_provider_id=provider_id, system_prompt=system,
                                               prompt=build_user(cands, interests, state.sent, owner, recent_chat))
        decided = parse(resp.completion_text or "", cands)
        chosen, final = apply_rules(cands, decided, state, rules, must_only)
        report = {"why": why, "thoughts": [(c.kind, d.model_said, d.decision, d.reason, d.message)
                                           for c, d in zip(cands, final)],
                  "sent": chosen.message if chosen else ""}
        if dry_run:
            return report

        stamp = now.isoformat()
        for c, d in zip(cands, final):
            state.thoughts.append({"at": stamp, "kind": c.kind, "about": c.content[:80], "model": d.model_said,
                                   "decision": d.decision, "reason": d.reason, "scores": d.scores,
                                   "message": d.message})
            if d.decision == "later":
                state.later.append({"at": stamp, "kind": c.kind, "text": d.message})
            if c.kind == "followup":
                state.followed_up.append(c.ref)
        state.mistakes = []  # each told now or kept for later
        state.considered_until = watermark
        if chosen is not None:
            if chosen.kind == "longing":
                state.longing_sent_for = chosen.ref
            await self._send(mem, state, chosen.kind, chosen.message)
        await asyncio.to_thread(state.save, core)
        logger.info(f"[thinking-of-you] {len(cands)} thoughts, sent: {chosen.kind if chosen else 'nothing'}")
        return report

    @staticmethod
    def _declined_longing_recently(state: State, now: datetime, hours: float = 3) -> bool:
        for t in reversed(state.thoughts):
            if t["kind"] == "longing":
                return (now - datetime.fromisoformat(t["at"])).total_seconds() < hours * 3600
        return False

    @staticmethod
    def _recent_chat(core, space: str, owner: str, now: datetime, hours: float = 24) -> list[str]:
        """The last lines of the owner's private chat (so she knows what they already talked about)."""
        if not space:
            return []
        out = []
        for ep in core.recent(space, 14):
            if (now - ep.occurred_at).total_seconds() > hours * 3600:
                continue
            who = owner if ep.kind == "message_in" else "我" if ep.kind == "message_out" else None
            if who:
                out.append(f"[{ep.occurred_at.astimezone():%H:%M}] {who}：{ep.content[:80].replace(chr(10), ' / ')}")
        return out

    @staticmethod
    def _dated_owner_memories(core) -> list:
        rows = core.store._conn.execute(
            "SELECT id FROM memory WHERE scope = 'owner' AND status = 'active' AND kind IN ('event', 'fact')"
            " AND text LIKE '%月%日%' ORDER BY created_at DESC LIMIT 200").fetchall()
        return [m for m in (core.memory(r[0]) for r in rows) if m is not None]

    async def _send(self, mem, state: State, kind: str, message: str) -> None:
        """Send line by line like a person typing, record it, and add it to AstrBot's history."""
        from memory_core import Episode

        from memory_core.now import jitter

        lines = [ln.strip() for ln in message.split("\n") if ln.strip()][:3]
        await asyncio.sleep(jitter(60, self._jitter(), self.rng))  # a moment to type, not the instant she decides
        for i, line in enumerate(lines):
            if i:
                await asyncio.sleep(self.rng.uniform(1.5, 3.5))
            await self.context.send_message(state.owner_umo, MessageChain([Plain(line)]))
        text = "\n".join(lines)
        now = _now()
        space = getattr(state, "owner_space", "")
        if space:
            await asyncio.to_thread(mem.core.record, Episode(
                kind="message_out", content=text, space=space, source="thinking-of-you", actor="self",
                meta={"proactive": kind}))
        try:  # so that when the owner answers, the conversation includes what she said
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(state.owner_umo)
            conv = await conv_mgr.get_conversation(state.owner_umo, cid) if cid else None
            if conv is not None:
                history = json.loads(conv.history or "[]")
                history.append({"role": "assistant", "content": text})
                await conv_mgr.update_conversation(state.owner_umo, cid, history=history)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[thinking-of-you] could not add to the conversation history: {exc}")
        state.sent.append({"at": now.isoformat(), "kind": kind, "text": text, "answered_at": ""})
        state.sent_today[kind] = state.sent_today.get(kind, 0) + 1
        state.unanswered += 1
        state.last_sent_at = now.isoformat()
        gap = jitter(self._rules().min_gap_minutes, self._jitter(), self.rng)
        state.next_after = (now + timedelta(minutes=gap)).isoformat()

    # ---------------------------------------------------------------- for other plugins (e.g. phone control)

    async def report_mistake(self, what: str, *, severity: str = "minor", affects_owner: bool = False) -> None:
        """Something she did went wrong. If it affects the owner she tells them at the next tick, otherwise she
        keeps it to tell them when they come back."""
        mem = self._mem()
        if mem is None:
            return

        def write():
            state = State.load(mem.core)
            state.mistakes.append({"at": _now().isoformat(), "what": what, "severity": severity,
                                   "affects_owner": affects_owner})
            state.save(mem.core)
        await asyncio.to_thread(write)

    # ---------------------------------------------------------------- owner commands

    def _console_denied(self, event: AstrMessageEvent) -> bool:
        mem = self._mem()
        if mem is not None and hasattr(mem, "_console_denied"):
            return mem._console_denied(event)
        event.stop_event()
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tov_log")
    async def cmd_log(self, event: AstrMessageEvent):
        """她最近心里想过什么、说了什么"""
        if self._console_denied(event):
            return
        mem = self._mem()
        state = await asyncio.to_thread(State.load, mem.core)
        names = {"longing": "想念", "share": "分享", "followup": "惦记", "mistake": "做错"}
        verdict = {"now": "说了", "later": "攒着", "no": "没说"}
        lines = [f"今天主动说了 {sum(state.sent_today.values())} 次，连着没回 {state.unanswered} 条"]
        for t in state.thoughts[-10:]:
            score = f" {sum(t['scores'].values())}分" if t.get("scores") else ""
            lines.append(f"· {t['at'][5:16]} {names.get(t['kind'], t['kind'])}{score} → {verdict.get(t['decision'])}"
                         f"（模型：{verdict.get(t['model'], '-')}）{t['about'][:30]}\n  {t['reason']}"
                         + (f"\n  「{t['message'][:60]}」" if t.get("message") else ""))
        if state.later:
            lines.append("攒着的：" + "；".join(i["text"][:30] for i in state.later))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tov_think")
    async def cmd_think(self, event: AstrMessageEvent):
        """试想一次：不管时间规则，看她现在会想什么、会不会开口（不发送，不改状态）"""
        if self._console_denied(event):
            return
        report = await self.think(dry_run=True)
        verdict = {"now": "现在说", "later": "攒着", "no": "不说"}
        lines = [f"（{report.get('why') or '规则上现在可以开口'}）"]
        for kind, model, final, reason, message in report.get("thoughts", []):
            lines.append(f"· {kind}：模型 {verdict.get(model, '-')} → 最终 {verdict.get(final)}；{reason}"
                         + (f"\n  「{message}」" if message else ""))
        if len(lines) == 1:
            lines.append("心里没什么特别想说的")
        yield event.plain_result("\n".join(lines))
