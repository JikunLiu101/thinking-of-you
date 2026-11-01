/* thinking-of-you dashboard. Her thoughts and messages are put in with textContent. */
(function () {
  "use strict";
  const K = window.PanelKit;
  const { el, clear, card, badge, empty, time, clock } = K;
  const $ = (id) => document.getElementById(id);
  const api = K.makeApi("主动找你");
  const KIND = { longing: "想念", share: "分享", followup: "惦记", mistake: "做错" };
  const VERDICT = { now: ["说了", "good"], later: ["攒着", "info"], no: ["没说", ""] };
  const SCORE = { related: "相关", feeling: "感受", timely: "时效", new: "新鲜", replyable: "好接话" };
  let owner = "主人";
  let watch = null;

  function verdict(v) {
    const [t, c] = VERDICT[v] || [v || "-", ""];
    return badge(t, c);
  }

  function render(d) {
    const notes = [];
    if (!d.memory_core) {
      clear($("notes"), el("div", { class: "note" }, "没装记忆核心插件：她没有可以想的东西，也不知道该找谁。"));
      clear($("cards")); clear($("later")); clear($("sent")); clear($("thoughts"));
      renderRules(d);
      return;
    }
    owner = d.owner || owner;
    if (!d.enabled) notes.push(el("div", { class: "note" }, "插件设置里关掉了：她不会自己去想，也不会主动发消息。"));
    if (!d.knows_where) notes.push(el("div", { class: "note" }, `还不知道去哪找${owner}：要${owner}先在私聊里跟她说过话。`));
    clear($("notes"), notes);

    const r = d.rules;
    clear($("cards"),
      card("今天主动说了", `${d.sent_today_total} / ${r.daily_cap}`,
        Object.entries(d.sent_today).map(([k, n]) => `${KIND[k] || k} ${n}`).join("、")),
      card("连着没回", `${d.unanswered} / ${r.max_unanswered}`, d.unanswered >= r.max_unanswered ? `等${owner}说话再说` : "",
        d.unanswered >= r.max_unanswered ? "warn" : ""),
      card("上次主动说", d.last_sent_at ? K.ago(d.last_sent_at) : "—", d.last_sent_at ? time(d.last_sent_at) : ""),
      card("下次最早", d.next_after && new Date(d.next_after) > new Date() ? clock(d.next_after) : "现在", "两次之间隔开一阵"),
      card("攒着的", String(d.later.length), "等他来了再说"));

    clear($("later"), d.later.length ? d.later.map((x) => el("div", { class: "item" },
      el("div", { class: "meta" }, time(x.at), badge(KIND[x.kind] || x.kind, "info")), x.text)) : empty("没有攒着的话"));
    $("later-clear").disabled = !d.later.length;

    clear($("sent"), d.sent.length ? d.sent.map((x) => el("div", { class: "item" },
      el("div", { class: "meta" }, time(x.at), badge(KIND[x.kind] || x.kind),
        x.answered_at ? badge(`${owner}回了`, "good") : badge("还没回", "warn")), x.text)) : empty("还没主动发过"));

    clear($("thoughts"), d.thoughts.length ? d.thoughts.map((t) => {
      const score = t.scores && Object.keys(t.scores).length
        ? el("div", { class: "small muted" }, Object.entries(t.scores).map(([k, v]) => `${SCORE[k] || k} ${v}`).join(" · ")
          + `（共 ${Object.values(t.scores).reduce((a, b) => a + b, 0)}）`) : null;
      const overruled = t.model && t.model !== t.decision
        ? el("span", { class: "small muted" }, `（模型想：${(VERDICT[t.model] || [t.model])[0]}，规则改了）`) : null;
      return el("div", { class: "item" },
        el("div", { class: "meta" }, time(t.at), badge(KIND[t.kind] || t.kind), verdict(t.decision), overruled),
        el("div", {}, t.about),
        t.reason ? el("div", { class: "thought" }, t.reason) : null,
        t.message ? el("div", {}, "「" + t.message + "」") : null,
        score);
    }) : empty("还没想过什么"));

    renderRules(d);
    if (d.thinking) watchThink();
  }

  function renderRules(d) {
    const r = d.rules;
    clear($("rules"), `每 ${r.tick_minutes} 分钟想一次；一天最多主动说 ${r.daily_cap} 次，两次至少隔 ${r.min_gap_minutes} 分钟，`
      + `连着 ${r.max_unanswered} 条没回就停；她睡觉时（或 ${r.quiet_start} 点到 ${r.quiet_end} 点）不打扰。`
      + `发到：${(d.platforms || []).join("、") || "（没有平台）"}。`);
  }

  function renderThink(s) {
    $("think-panel").hidden = false;
    $("think").disabled = !!s.thinking;
    if (s.thinking || s.state === "thinking") { clear($("think-result"), badge("在想", "info"), " 要调一次模型，十几秒…"); return; }
    if (s.state === "failed") { clear($("think-result"), badge("没想成", "bad"), " " + s.error); return; }
    if (s.state !== "done") return;
    clear($("think-result"),
      el("div", { class: "small muted" }, s.why ? `按规则现在${s.why}，这次只看会想什么。` : "按规则现在可以开口。"),
      s.rows.length ? s.rows.map((x) => el("div", { class: "item", style: "margin-top:6px" },
        el("div", { class: "meta" }, badge(KIND[x.kind] || x.kind), "模型：", verdict(x.model), "最终：", verdict(x.decision)),
        x.reason ? el("div", { class: "thought" }, x.reason) : null,
        x.message ? el("div", {}, "「" + x.message + "」") : null)) : empty("心里没什么特别想说的"));
  }

  function watchThink() {
    if (watch) return;
    watch = setInterval(async () => {
      const s = await api("GET", "think_status").catch(() => null);
      if (!s) return;
      renderThink(s);
      if (!s.thinking) { clearInterval(watch); watch = null; }
    }, 2500);
  }

  async function load() { render(await api("GET", "overview")); }

  $("think").addEventListener("click", K.busy($("think"), async () => {
    await api("POST", "think");
    renderThink({ thinking: true });
    watchThink();
  }));

  // The view runs in a sandbox without dialogs: confirm by clicking a second time.
  let armed = null;
  $("later-clear").addEventListener("click", async () => {
    const b = $("later-clear");
    if (!armed) {
      b.textContent = "再点一次确认";
      b.classList.add("danger");
      armed = setTimeout(() => { armed = null; b.textContent = "都不说了"; b.classList.remove("danger"); }, 5000);
      return;
    }
    clearTimeout(armed); armed = null; b.textContent = "都不说了"; b.classList.remove("danger");
    await api("POST", "later_clear");
    load();
  });

  $("refresh").addEventListener("click", () => load().catch(() => {}));
  K.onReady("主动找你", () => load().catch(() => {}));
})();
