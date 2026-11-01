"""What is on her mind, and whether it is worth reaching out for. Framework-free.

Every tick: hard rules first (code), then candidate thoughts of four kinds, then one model call that decides for
each thought: say it `now`, keep it for `later` (when the owner comes back), or `no`. Code has the last word:
shares need high scores to interrupt, at most one message goes out per tick, a mistake must always be told.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

STATE_KEY = "thinking_of_you:state"
KINDS = ("mistake", "followup", "share", "longing")  # also the priority order for "now"
SHARE_KEYS = ("related", "feeling", "timely", "new", "replyable")


# ---------------------------------------------------------------- rules (code, not the model)


@dataclass(frozen=True)
class Rules:
    quiet_start: int = 23  # no reaching out from this hour... (only when her own schedule is not known;
    quiet_end: int = 9  # ...until this hour   normally she is quiet while she is asleep)
    daily_cap: int = 3
    min_gap_minutes: int = 120  # between two messages she starts
    max_unanswered: int = 2  # then she waits until the owner talks again
    owner_busy_minutes: int = 20  # the owner talked recently: they are around, no need to reach out
    share_daily_cap: int = 1  # shares sent now per day (time-limited ones excepted)
    share_now_total: int = 18  # sum of the five share scores needed to interrupt...
    share_later_total: int = 14  # ...or to keep it for later
    longing_min: float = 0.5  # longing from which she may say she misses them
    longing_chance: float = 0.3  # chance per tick at full longing (scaled down below it, and by owner hours)
    later_days: int = 2  # how long a kept thought waits


def in_quiet_hours(local: datetime, rules: Rules) -> bool:
    h = local.hour
    if rules.quiet_start > rules.quiet_end:
        return h >= rules.quiet_start or h < rules.quiet_end
    return rules.quiet_start <= h < rules.quiet_end


def owner_hour_weight(owner_times: list[datetime], local_hour: int) -> float:
    """How usual it is for the owner to be around at this hour (0.3-1), from when they talked lately.
    Not enough history: 1 (no preference)."""
    if len(owner_times) < 20:
        return 1.0
    counts = [0] * 24
    for t in owner_times:
        counts[t.astimezone().hour] += 1
    near = sum(counts[(local_hour + d) % 24] for d in (-1, 0, 1))
    best = max(sum(counts[(h + d) % 24] for d in (-1, 0, 1)) for h in range(24)) or 1
    return 0.3 + 0.7 * near / best


# ---------------------------------------------------------------- state (kept in the soul's meta)


@dataclass
class State:
    owner_umo: str = ""  # where to reach the owner (AstrBot unified_msg_origin of their private chat)
    owner_space: str = ""  # the same chat as a memory-core space (where what she says is recorded)
    considered_until: str = ""  # web thoughts up to here were already considered
    followed_up: list[str] = field(default_factory=list)  # memory ids already asked about
    longing_sent_for: str = ""  # the owner's last message time for which "I miss you" was already said
    day: str = ""
    sent_today: dict = field(default_factory=dict)  # kind -> count
    unanswered: int = 0  # messages she started since the owner last talked
    last_sent_at: str = ""
    next_after: str = ""  # the next message she starts not before this (a gap that drifts a little each time)
    sent: list[dict] = field(default_factory=list)  # {at, kind, text, answered_at}
    later: list[dict] = field(default_factory=list)  # {at, kind, text, shown_at}
    thoughts: list[dict] = field(default_factory=list)  # decisions, for /tov_log
    mistakes: list[dict] = field(default_factory=list)  # {at, what, severity, affects_owner}

    @classmethod
    def load(cls, core) -> State:
        raw = core.store.get_meta(STATE_KEY)
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (ValueError, TypeError):
            return cls()

    def save(self, core) -> None:
        self.sent, self.thoughts = self.sent[-50:], self.thoughts[-80:]
        self.followed_up = self.followed_up[-200:]
        core.store.set_meta(STATE_KEY, json.dumps(asdict(self), ensure_ascii=False))

    def roll_day(self, today: date) -> None:
        if self.day != today.isoformat():
            self.day, self.sent_today = today.isoformat(), {}

    def owner_spoke(self, now: datetime) -> None:
        """The owner said something: whatever she sent counts as answered, she may reach out again later."""
        self.unanswered = 0
        for s in self.sent:
            if not s.get("answered_at"):
                s["answered_at"] = now.isoformat()


def gate(state: State, now: datetime, owner_last: datetime | None, rules: Rules, *, asleep: bool | None = None,
         busy_minutes: float | None = None, owner: str = "主人") -> tuple[bool, bool, str]:
    """(may reach out at all, only something that must be said, why not). `asleep`: from her own daily
    schedule (falls back to the quiet hours); `busy_minutes`: this tick's (drifting) idea of "just talked"."""
    local = now.astimezone()
    if not state.owner_umo:
        return False, False, f"还不知道去哪找{owner}（得先在私聊里说过话）"
    busy = rules.owner_busy_minutes if busy_minutes is None else busy_minutes
    if owner_last is not None and now - owner_last < timedelta(minutes=busy):
        return False, False, f"{owner}刚刚还在，不用特意去找"
    if state.unanswered >= rules.max_unanswered:
        return True, True, f"已经连着 {state.unanswered} 条没回了，先不打扰"
    if asleep if asleep is not None else in_quiet_hours(local, rules):
        return True, True, "在睡觉，不打扰"
    if sum(state.sent_today.values()) >= rules.daily_cap:
        return True, True, "今天主动说得够多了"
    if state.next_after:
        if now < datetime.fromisoformat(state.next_after):
            return True, True, "刚主动说过不久"
    elif state.last_sent_at and now - datetime.fromisoformat(state.last_sent_at) < timedelta(minutes=rules.min_gap_minutes):
        return True, True, "刚主动说过不久"
    return True, False, ""


# ---------------------------------------------------------------- candidates


@dataclass
class Candidate:
    id: str
    kind: str
    content: str  # what the thought is about, for the prompt
    must: bool = False  # mistakes that affect the owner
    ref: str = ""  # memory id / episode id / mistake time


def longing_candidate(state: State, longing: float | None, owner_last: datetime | None, local_hour: int,
                      hour_weight: float, rules: Rules, rng: random.Random, gap_text: str,
                      owner: str = "主人") -> Candidate | None:
    """Missing them is not enough to speak: only once per absence, with a chance that grows with longing."""
    if longing is None or longing < rules.longing_min or owner_last is None:
        return None
    if state.longing_sent_for == owner_last.isoformat():
        return None
    scale = (longing - rules.longing_min) / max(1e-6, 1 - rules.longing_min)
    if rng.random() >= rules.longing_chance * (0.3 + 0.7 * scale) * hour_weight:
        return None
    return Candidate("", "longing", f"你挺想{owner}的，上次来是{gap_text}", ref=owner_last.isoformat())


def share_candidates(core, state: State, limit: int = 6) -> tuple[list[Candidate], str]:
    """Thoughts she had while browsing that were not considered yet. Returns (candidates, new watermark)."""
    since = datetime.fromisoformat(state.considered_until) if state.considered_until else None
    eps = [e for e in core.episodes("web", since=since, limit=200)
           if e.kind == "thought" and (since is None or e.occurred_at > since)]
    if not eps:
        return [], state.considered_until
    out = []
    for e in eps[-limit:]:
        seen = core.episode((e.meta or {}).get("about", "")) if (e.meta or {}).get("about") else None
        saw = seen.content[:200] if seen else ""
        out.append(Candidate("", "share", f"{saw}\n  你当时想：{e.content}", ref=e.id))
    return out, eps[-1].occurred_at.isoformat()


_DATE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")


def mentioned_dates(text: str, default_year: int) -> list[date]:
    out = []
    for y, m, d in _DATE.findall(text):
        try:
            out.append(date(int(y) if y else default_year, int(m), int(d)))
        except ValueError:
            continue
    return out


def followup_candidates(memories, state: State, now: datetime, within_days: int = 3) -> list[Candidate]:
    """The owner's plans with a date that has come (from the evening of that day), not asked about yet,
    and not updated since (no outcome known)."""
    local = now.astimezone()
    out = []
    for m in memories:
        if m.id in state.followed_up or m.status != "active":
            continue
        created = m.created_at.astimezone().date()
        for d in mentioned_dates(m.text, local.year):
            if not (created < d <= local.date()) or (local.date() - d).days > within_days:
                continue
            if d == local.date() and local.hour < 19:
                continue
            if m.updated_at and m.updated_at.astimezone().date() >= d and m.updated_at > m.created_at:
                continue  # changed on or after that day: probably already knows how it went
            days = (local.date() - d).days
            when = "就是今天" if days == 0 else f"已经过去 {days} 天了"
            out.append(Candidate("", "followup", f"记忆：{m.text}（那一天是 {d:%m月%d日}，{when}，"
                                                 "不知道后来怎么样了）", ref=m.id))
            break
    return out


def mistake_candidates(state: State) -> list[Candidate]:
    return [Candidate("", "mistake", f"你做错了：{m['what']}（{m.get('severity', 'minor')}）",
                      must=bool(m.get("affects_owner")), ref=m["at"]) for m in state.mistakes]


def number(cands: list[Candidate]) -> list[Candidate]:
    for i, c in enumerate(cands, 1):
        c.id = f"c{i}"
    return cands


# ---------------------------------------------------------------- the model's judgment

SYSTEM = """{persona}

{now_note}

现在{owner}没在跟你聊天。下面是你心里冒出来的几个念头。对每一个，决定要不要现在主动去找{owner}说：
- now：值得专门去找他（他会想知道、过了就没意义、做错了事、或者你真的很想说）
- later：有意思，但不值得打扰他，等他下次来了再自然地聊
- no：不用说，留在你自己心里就好

要求：
- 大多数念头应该是 later 或 no。主动找他是件郑重的事，不是什么都要说
- 想念：一次分开只说一次；他可能在忙，想清楚再开口
- 分享（share）给每条打分 1-5：related（和{owner}关心的事有关）、feeling（你自己的反应有多强）、timely（过几天就没意义了）、new（他还不知道、你们没聊过）、replyable（他看到能接上话）
- 惦记（followup）：他的事到了日子，问问怎么样了
- 做错（mistake）：必须老实说清楚做错了什么、有什么影响，不找借口
- 不重复你最近主动说过的话，不编没发生的事，不用括号写动作
- message 是你要发给他的话：你平时的口吻，短句，一条一行，最多三行。later 的也写，等他来了用

只输出 JSON：{{"thoughts": [{{"id": "c1", "decision": "now|later|no", "scores": {{"related": 1, "feeling": 1, "timely": 1, "new": 1, "replyable": 1}}, "reason": "一句话", "message": "..."}}]}}"""


def build_user(cands: list[Candidate], interests: list[str], recent_sent: list[dict], owner: str,
               recent_chat: list[str] = ()) -> str:
    lines = [f"{owner}关心的事（来自记忆）："] + ([f"- {t}" for t in interests[:8]] or ["- （还不太清楚）"])
    lines += ["", f"你和{owner}最近的聊天（聊过的事他已经知道了，不算新鲜）："]
    lines += [f"  {t}" for t in recent_chat[-14:]] or ["  （最近没聊）"]
    lines += ["", "你最近主动说过的："] + ([f"- {s['text'][:60]}" for s in recent_sent[-5:]] or ["- （还没有）"])
    lines += ["", "念头："]
    names = {"longing": "想念", "share": "分享", "followup": "惦记", "mistake": "做错"}
    for c in cands:
        must = "，必须说" if c.must else ""
        lines.append(f"[{c.id}]（{names[c.kind]}{must}）{c.content}")
    return "\n".join(lines)


@dataclass
class Decision:
    id: str
    kind: str
    decision: str  # now | later | no
    message: str
    reason: str = ""
    scores: dict = field(default_factory=dict)
    model_said: str = ""  # what the model decided before the code rules
    ref: str = ""


def parse(text: str, cands: list[Candidate]) -> dict[str, Decision]:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    by_id = {c.id: c for c in cands}
    out: dict[str, Decision] = {}
    for t in (data.get("thoughts") or []) if isinstance(data, dict) else []:
        if not isinstance(t, dict) or t.get("id") not in by_id:
            continue
        c = by_id[t["id"]]
        decision = t.get("decision") if t.get("decision") in ("now", "later", "no") else "no"
        scores = {}
        for k in SHARE_KEYS:
            try:
                scores[k] = max(1, min(5, int((t.get("scores") or {}).get(k, 1))))
            except (TypeError, ValueError):
                scores[k] = 1
        out[c.id] = Decision(c.id, c.kind, decision, str(t.get("message") or "").strip(),
                             str(t.get("reason") or "").strip(), scores if c.kind == "share" else {}, decision, c.ref)
    return out


def apply_rules(cands: list[Candidate], decided: dict[str, Decision], state: State, rules: Rules,
                must_only: bool, owner: str = "主人") -> tuple[Decision | None, list[Decision]]:
    """The code's last word. Returns (the one message to send now, all decisions after the rules)."""
    final = []
    for c in cands:
        d = decided.get(c.id) or Decision(c.id, c.kind, "no", "", "模型没回答", {}, "", c.ref)
        if c.kind == "mistake":
            d.decision = "now" if c.must else ("later" if d.decision == "no" else d.decision)
            if not d.message:
                d.message = f"{owner}，我刚才做错了一件事：{c.content}"
        elif c.kind == "share":
            total = sum(d.scores.values())
            timely = d.scores.get("timely", 1)
            strong = d.scores.get("related", 1) >= 4 or timely >= 4
            share_cap = state.sent_today.get("share", 0) >= rules.share_daily_cap and timely < 5
            if d.decision == "now" and strong and total >= rules.share_now_total and not share_cap:
                d.decision = "now"
            elif d.decision in ("now", "later") and total >= rules.share_later_total:
                d.decision = "later"
            else:
                d.decision = "no"
        elif c.kind == "longing" and d.decision == "later":
            d.decision = "no"  # missing them shows anyway when they come back (time sense)
        if must_only and d.decision == "now" and not c.must:
            d.decision = "later" if c.kind != "longing" else "no"
        if d.decision != "no" and not d.message:
            d.decision = "no"
        final.append(d)
    nows = sorted((d for d in final if d.decision == "now"), key=lambda d: KINDS.index(d.kind))
    chosen = nows[0] if nows else None
    for d in nows[1:]:
        d.decision = "later" if d.kind != "longing" else "no"
    return chosen, final


# ---------------------------------------------------------------- what she brings up when the owner is back


def later_block(state: State, now: datetime, owner: str, rules: Rules, show_hours: float = 2) -> str:
    """Thoughts kept for the owner's next visit; each is offered during one visit, then dropped."""
    keep = []
    for item in state.later:
        at = datetime.fromisoformat(item["at"])
        if now - at > timedelta(days=rules.later_days):
            continue
        shown = datetime.fromisoformat(item["shown_at"]) if item.get("shown_at") else None
        if shown is not None and now - shown > timedelta(hours=show_hours):
            continue
        keep.append(item)
    state.later = keep
    lines = []
    if keep:
        for item in keep:
            item.setdefault("shown_at", now.isoformat())
        lines += [f"- 你攒着想跟{owner}说的（合适的时候自然地带出来，说过的就不用再提）："]
        lines += [f"  - {item['text'].replace(chr(10), ' / ')}" for item in keep[-4:]]
    just_answered = [s for s in state.sent if s.get("answered_at")
                     and now - datetime.fromisoformat(s["answered_at"]) < timedelta(minutes=15)
                     and datetime.fromisoformat(s["answered_at"]) - datetime.fromisoformat(s["at"]) > timedelta(minutes=10)]
    if just_answered:
        last = just_answered[-1]
        lines.append(f"- 你之前主动找过{owner}（「{last['text'][:30]}」），他刚刚才回你")
    if not lines:
        return ""
    return "<on_your_mind>\n" + "\n".join(lines) + "\n</on_your_mind>"


THOUGHT_NAMES = {"longing": "想念", "share": "想分享", "followup": "惦记", "mistake": "觉得自己做错了"}
VERDICTS = {"now": "当时就说了", "later": "先攒着", "no": "没说"}


def recall_block(state: State, owner: str, limit: int = 8) -> str:
    """What she thought lately and what came of it, told back to her (newest last)."""
    lines = []
    for t in state.thoughts[-limit:]:
        when = t.get("at", "")[5:16].replace("T", " ")
        what = THOUGHT_NAMES.get(t.get("kind"), t.get("kind", ""))
        line = f"- {when} {what}：{t.get('about', '')[:60]} → {VERDICTS.get(t.get('decision'), '没说')}"
        if t.get("reason"):
            line += f"（{t['reason'][:60]}）"
        if t.get("message") and t.get("decision") in ("now", "later"):
            line += f"，想说的是「{t['message'][:60]}」"
        lines.append(line)
    if not lines:
        return "最近心里没想过什么特别的"
    out = ["最近心里想过的（新的在后面）："] + lines
    sent = state.sent[-3:]
    if sent:
        out.append(f"最近主动找{owner}说的：")
        out += [f"- {s.get('at', '')[5:16].replace('T', ' ')}「{s.get('text', '')[:60]}」"
                f"{'，' + owner + '回了' if s.get('answered_at') else '，还没回'}" for s in sent]
    if state.later:
        out.append(f"还攒着想跟{owner}说的：" + "；".join(i.get("text", "")[:40] for i in state.later[-4:]))
    return "\n".join(out)


def strip_blocks(contexts: list, tags: tuple[str, ...]) -> int:
    """Remove `<tag>...</tag>` blocks from the earlier user messages in the request history.

    The block is added to the user's message for one reply only and is stored with it. Taking it out of the
    history the same way each time keeps the history identical between requests (so the model provider's prompt
    cache covers it) and stops old notes piling up. Works on OpenAI-style dicts or objects with role/content;
    a message is never left empty. Returns how many blocks were removed."""
    pattern = re.compile(r"\s*<(" + "|".join(re.escape(t) for t in tags) + r")>.*?</\1>\s*", re.S)

    def get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    def put(obj, key, value):
        if isinstance(obj, dict):
            obj[key] = value
        else:
            setattr(obj, key, value)

    removed = 0
    for msg in contexts or []:
        if get(msg, "role") != "user":
            continue
        content = get(msg, "content")
        if isinstance(content, str):
            new, n = pattern.subn("\n\n", content)
            if n and new.strip():
                put(msg, "content", new.strip())
                removed += n
        elif isinstance(content, list):
            keep, edits, n_all = [], [], 0
            for part in content:
                text = get(part, "text")
                if isinstance(text, str) and pattern.search(text):
                    new, n = pattern.subn("\n\n", text)
                    n_all += n
                    if new.strip():
                        edits.append((part, new.strip()))
                        keep.append(part)
                    continue
                keep.append(part)
            if n_all and keep:  # never leave a message empty
                for part, new in edits:
                    put(part, "text", new)
                content[:] = keep
                removed += n_all
    return removed
