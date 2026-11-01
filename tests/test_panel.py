"""The dashboard's Web API on a stand-in plugin: what it shows, and letting go of the kept thoughts."""

import asyncio
from types import SimpleNamespace as NS

from tov import panel as panel_mod
from tov.mind import Rules, State


class Store:
    def __init__(self):
        self.meta = {}

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value


class FakePlugin:
    def __init__(self, with_memory=True):
        self.cfg = {"enabled": True}
        self.context = NS(register_web_api=lambda *a: None)
        self._lock = asyncio.Lock()
        self.mem = NS(owner_title="主人", core=NS(store=Store())) if with_memory else None

    def _mem(self): return self.mem
    def _rules(self): return Rules()
    def _platforms(self): return {"telegram"}

    async def think(self, dry_run=False):
        assert dry_run
        return {"why": "", "thoughts": [("share", "now", "later", "先攒着", "你看这个")]}


def run(coro):
    return asyncio.run(coro)


def test_overview_without_memory_core():
    p = panel_mod.Panel(FakePlugin(with_memory=False))
    d = run(p._guarded(p.overview)())["data"]
    assert d["memory_core"] is False and d["rules"]["daily_cap"] == 3 and "thoughts" not in d


def test_overview_and_letting_go():
    plugin = FakePlugin()
    st = State(day="2000-01-01", sent_today={"share": 2}, later=[{"at": "t", "kind": "share", "text": "你看"}],
               thoughts=[{"at": "t1", "kind": "longing"}, {"at": "t2", "kind": "share"}])
    st.save(plugin.mem.core)
    p = panel_mod.Panel(plugin)
    d = run(p._guarded(p.overview)())["data"]
    assert d["sent_today_total"] == 0  # yesterday's count does not show as today's
    assert [t["at"] for t in d["thoughts"]] == ["t2", "t1"] and len(d["later"]) == 1
    assert run(p._guarded(p.later_clear)())["data"] == {"cleared": 1}
    assert State.load(plugin.mem.core).later == []


def test_think_runs_in_the_background():
    async def go():
        p = panel_mod.Panel(FakePlugin())
        assert (await p._guarded(p.think)())["data"] == {"started": True}
        await p.thinking
        return await p.think_status()

    s = run(go())
    assert s["state"] == "done" and not s["thinking"]
    assert s["rows"] == [{"kind": "share", "model": "now", "decision": "later", "reason": "先攒着", "message": "你看这个"}]
