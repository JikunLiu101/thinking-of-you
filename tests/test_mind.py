import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from tov.mind import (
    Candidate, Rules, State, apply_rules, build_user, followup_candidates, gate, in_quiet_hours, later_block,
    longing_candidate, mentioned_dates, number, owner_hour_weight, parse,
)

NOW = datetime(2026, 11, 7, 20, 0).astimezone()
R = Rules()


def ready_state(**kw) -> State:
    return State(owner_umo="webchat:FriendMessage:x", owner_space="webchat:private:o", day=NOW.date().isoformat(),
                 **kw)


# ---------------------------------------------------------------- hard rules


def test_gate():
    owner_last = NOW - timedelta(hours=5)
    assert gate(State(), NOW, owner_last, R)[0] is False  # nowhere to reach the owner yet
    assert gate(ready_state(), NOW, owner_last, R) == (True, False, "")
    assert gate(ready_state(), NOW, NOW - timedelta(minutes=5), R)[0] is False  # the owner is around
    assert gate(ready_state(unanswered=2), NOW, owner_last, R)[1] is True  # only must-say things
    night = NOW.replace(hour=23, minute=30)
    assert gate(ready_state(), night, owner_last, R)[1] is True
    assert in_quiet_hours(NOW.replace(hour=3), R) and not in_quiet_hours(NOW.replace(hour=10), R)
    s = ready_state(sent_today={"share": 1, "longing": 2})
    assert gate(s, NOW, owner_last, R)[1] is True  # daily cap
    s = ready_state(last_sent_at=(NOW - timedelta(minutes=30)).isoformat())
    assert gate(s, NOW, owner_last, R)[1] is True  # too soon after the last one


def test_owner_hours():
    assert owner_hour_weight([], 15) == 1.0
    evenings = [NOW.replace(hour=21) - timedelta(days=d) for d in range(30)]
    assert owner_hour_weight(evenings, 21) == 1.0
    assert owner_hour_weight(evenings, 10) == pytest.approx(0.3)


# ---------------------------------------------------------------- candidates


def test_longing_once_per_absence_and_by_chance():
    last = NOW - timedelta(days=2)
    always = random.Random(0)
    always.random = lambda: 0.0
    never = random.Random(0)
    never.random = lambda: 0.999
    s = ready_state()
    assert longing_candidate(s, 0.3, last, 20, 1.0, R, always, "2 天前") is None  # not missing them enough
    assert longing_candidate(s, 0.9, last, 20, 1.0, R, never, "2 天前") is None  # not this time
    c = longing_candidate(s, 0.9, last, 20, 1.0, R, always, "2 天前")
    assert c.kind == "longing" and "2 天前" in c.content and c.ref == last.isoformat()
    s.longing_sent_for = last.isoformat()
    assert longing_candidate(s, 0.9, last, 20, 1.0, R, always, "2 天前") is None  # already said this absence


@dataclass
class M:
    id: str
    text: str
    created_at: datetime
    updated_at: datetime
    status: str = "active"


def test_followups():
    made = NOW - timedelta(days=7)
    dentist = M("m1", "爸爸2026年11月7日要去看牙医", made, made)
    old = M("m2", "爸爸10月20日去游泳", made - timedelta(days=30), made - timedelta(days=30))
    future = M("m3", "爸爸11月20日出差", made, made)
    known = M("m4", "爸爸11月6日要考试，考得不错", made, NOW - timedelta(hours=20))
    out = followup_candidates([dentist, old, future, known], ready_state(), NOW)
    assert [c.ref for c in out] == ["m1"]
    assert followup_candidates([dentist], ready_state(), NOW.replace(hour=15)) == []  # not that evening yet
    assert followup_candidates([dentist], ready_state(followed_up=["m1"]), NOW) == []  # asked already
    assert mentioned_dates("10月3日和2027年1月2日", 2026) == [date(2026, 10, 3), date(2027, 1, 2)]


# ---------------------------------------------------------------- the model's answer and the code's last word


def _cands():
    return number([
        Candidate("", "share", "在B站看到《塔露拉上岛》\n  你当时想：好帅"),
        Candidate("", "share", "在IT之家看到《某手机发布》\n  你当时想：哦"),
        Candidate("", "longing", "你挺想爸爸的，他上次来是 2 天前"),
        Candidate("", "followup", "记忆：爸爸11月7日看牙医", ref="m1"),
    ])


def _answer(**over):
    base = {
        "c1": {"decision": "now", "scores": {"related": 5, "feeling": 4, "timely": 4, "new": 4, "replyable": 4},
               "message": "爸爸！塔露拉上岛了", "reason": "爸爸玩方舟"},
        "c2": {"decision": "later", "scores": {"related": 1, "feeling": 2, "timely": 2, "new": 3, "replyable": 2},
               "message": "看到个新手机", "reason": "一般"},
        "c3": {"decision": "now", "message": "爸爸，想你了", "reason": "两天没来"},
        "c4": {"decision": "now", "message": "牙看得怎么样呀", "reason": "今天看牙医"},
    }
    for k, v in over.items():
        base[k].update(v)
    return json.dumps({"thoughts": [{"id": k, **v} for k, v in base.items()]}, ensure_ascii=False)


def test_one_message_at_a_time_by_priority():
    cands = _cands()
    chosen, final = apply_rules(cands, parse(_answer(), cands), ready_state(), R, must_only=False)
    assert chosen.kind == "followup" and chosen.message == "牙看得怎么样呀"
    d = {x.id: x.decision for x in final}
    assert d == {"c1": "later", "c2": "no", "c3": "no", "c4": "now"}  # the rest waits; missing-you is not kept


def test_share_needs_high_scores_to_interrupt():
    cands = _cands()[:1]
    low = _answer(c1={"scores": {"related": 3, "feeling": 3, "timely": 3, "new": 3, "replyable": 3}})
    chosen, final = apply_rules(cands, parse(low, cands), ready_state(), R, must_only=False)
    assert chosen is None and final[0].decision == "later"  # 15: keep it for the next visit
    capped = ready_state(sent_today={"share": 1})
    chosen, _ = apply_rules(cands, parse(_answer(), cands), capped, R, must_only=False)
    assert chosen is None  # one share a day...
    urgent = _answer(c1={"scores": {"related": 5, "feeling": 4, "timely": 5, "new": 4, "replyable": 4}})
    chosen, _ = apply_rules(cands, parse(urgent, cands), capped, R, must_only=False)
    assert chosen is not None  # ...unless it will not keep


def test_mistakes_must_be_told():
    cands = number([Candidate("", "mistake", "你做错了：发错了消息", must=True),
                    Candidate("", "share", "x")])
    answer = json.dumps({"thoughts": [{"id": "c1", "decision": "no", "message": ""},
                                      {"id": "c2", "decision": "now", "message": "看这个",
                                       "scores": {k: 5 for k in ("related", "feeling", "timely", "new", "replyable")}}]})
    chosen, final = apply_rules(cands, parse(answer, cands), ready_state(), R, must_only=True)
    assert chosen.kind == "mistake" and "做错" in chosen.message
    assert final[1].decision == "later"  # quiet hours / limits: everything else waits


def test_garbage_from_the_model_says_nothing():
    cands = _cands()
    chosen, final = apply_rules(cands, parse("我不知道", cands), ready_state(), R, must_only=False)
    assert chosen is None and all(d.decision == "no" for d in final)


def test_prompt_lists_interests_and_recent():
    text = build_user(_cands(), ["爸爸喜欢明日方舟"], [{"text": "爸爸，想你了"}], "爸爸")
    assert "- 爸爸喜欢明日方舟" in text and "- 爸爸，想你了" in text and "[c3]（想念）" in text
    assert "（还不太清楚）" in build_user(_cands(), [], [], "爸爸")


# ---------------------------------------------------------------- when the owner is back


def test_later_and_answered():
    s = ready_state(later=[{"at": (NOW - timedelta(hours=3)).isoformat(), "kind": "share", "text": "塔露拉上岛了"},
                           {"at": (NOW - timedelta(days=3)).isoformat(), "kind": "share", "text": "太旧了"}],
                    sent=[{"at": (NOW - timedelta(hours=2)).isoformat(), "kind": "longing", "text": "爸爸，想你了",
                           "answered_at": ""}])
    s.owner_spoke(NOW)
    block = later_block(s, NOW, "爸爸", R)
    assert "塔露拉上岛了" in block and "太旧了" not in block and "他刚刚才回你" in block
    assert s.unanswered == 0
    # offered during one visit, then gone
    assert later_block(s, NOW + timedelta(hours=3), "爸爸", R) == ""


def test_gate_follows_her_schedule_and_drifting_gap():
    owner_last = NOW - timedelta(hours=5)
    assert gate(ready_state(), NOW, owner_last, R, asleep=True)[1] is True  # asleep: only must-say things
    late = NOW.replace(hour=23, minute=30)
    assert gate(ready_state(), late, owner_last, R, asleep=False) == (True, False, "")  # awake a bit later tonight
    s = ready_state(last_sent_at=(NOW - timedelta(minutes=130)).isoformat(),
                    next_after=(NOW + timedelta(minutes=10)).isoformat())
    assert gate(s, NOW, owner_last, R)[1] is True  # this time the gap drifted to 140 minutes
    s.next_after = (NOW - timedelta(minutes=1)).isoformat()
    assert gate(s, NOW, owner_last, R)[1] is False
    near = NOW - timedelta(minutes=22)
    assert gate(ready_state(), NOW, near, R, busy_minutes=24)[0] is False
    assert gate(ready_state(), NOW, near, R, busy_minutes=17)[0] is True


def test_recall_block():
    from tov.mind import State, recall_block
    s = State()
    assert recall_block(s, "老板") == "最近心里没想过什么特别的"
    s.thoughts = [{"at": "2026-10-30T21:05:00+08:00", "kind": "longing", "about": "老板一天没来", "decision": "later",
                   "reason": "他在忙", "message": "你今天好忙呀"},
                  {"at": "2026-10-30T22:00:00+08:00", "kind": "share", "about": "B站新番", "decision": "no",
                   "reason": "不新鲜", "message": ""}]
    s.sent = [{"at": "2026-10-30T20:00:00+08:00", "text": "在吗", "answered_at": "x"}]
    s.later = [{"text": "你今天好忙呀"}]
    b = recall_block(s, "老板")
    assert "10-30 21:05 想念：老板一天没来 → 先攒着（他在忙），想说的是「你今天好忙呀」" in b
    assert "B站新番 → 没说（不新鲜）" in b and "「在吗」，老板回了" in b and "还攒着想跟老板说的" in b


def test_strip_blocks_keeps_history_stable():
    from tov.mind import strip_blocks
    block = "<on_your_mind>\n- note\n</on_your_mind>"
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": block}]},
               {"role": "assistant", "content": "ok"},
               {"role": "user", "content": f"again\n\n{block}"},
               {"role": "user", "content": [{"type": "text", "text": block}]}]
    assert strip_blocks(history, ("on_your_mind",)) == 2
    assert history[0]["content"] == [{"type": "text", "text": "hi"}]
    assert history[2]["content"] == "again" and len(history[3]["content"]) == 1
    assert strip_blocks(history, ("on_your_mind",)) == 0
