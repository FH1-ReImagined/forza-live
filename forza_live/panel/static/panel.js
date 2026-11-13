// The Forza Live operator panel (forza_live/panel/routes.py). Every value shown comes from the
// server and much of it is players' or server owners' text: it is only ever put on the page as
// text: el() makes text nodes, and no markup is ever parsed from a value.
"use strict";

const API = "/operator/api/";
const KEY = "forzaLivePanelToken";
const TABS = [
  ["status", "Status"], ["messages", "Messages"], ["gifts", "Gifts"], ["players", "Players"],
  ["bans", "Bans and chat"], ["scores", "Scores"], ["photos", "Photos"], ["videos", "Videos"],
  ["designs", "Designs"],
  ["servers", "Servers"], ["relays", "Relays"], ["challenges", "Challenges"],
  ["events", "Events"], ["audit", "Audit log"],
];
const TIME_KEYS = /^(at|time|sent|expires|until|seen|last_seen|last_logon|first|submitted|decidedAt|takenAt|approvedAt|createdAt|starts|ends|last|done)$/;
let panel = {};       // the overview's "panel" block (gift cap, accounts admin link...)
let blobs = [];       // object URLs of the images shown, revoked when the view changes

// ---- the page ----

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = String(v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

function token() { return sessionStorage.getItem(KEY) || ""; }

function when(t) {
  if (typeof t !== "number" || t < 1e9) return t === null || t === undefined ? "" : String(t);
  return new Date(t * 1000).toLocaleString();
}

function duration(s) {
  if (typeof s !== "number") return "";
  if (s < 120) return s + " s";
  if (s < 7200) return Math.round(s / 60) + " min";
  if (s < 172800) return Math.round(s / 3600) + " h";
  return Math.round(s / 86400) + " days";
}

function fmt(key, v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number" && TIME_KEYS.test(key)) return when(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// A readable view of any answer: objects as key/value lists, lists of objects as tables.
function view(v, key) {
  if (Array.isArray(v)) {
    if (!v.length) return el("span", {class: "muted", text: "none"});
    if (v.every(x => x && typeof x === "object" && !Array.isArray(x))) return table(v);
    return el("span", {}, v.map(x => fmt(key || "", x)).join(", "));
  }
  if (v && typeof v === "object") {
    const dl = el("dl", {class: "kv"});
    for (const [k, x] of Object.entries(v)) {
      dl.append(el("dt", {text: k}), el("dd", {}, x && typeof x === "object" ? view(x, k) : fmt(k, x)));
    }
    return dl;
  }
  return el("span", {text: fmt(key || "", v)});
}

function table(rows, columns, actions) {
  if (!rows.length) return el("p", {class: "muted", text: "None."});
  const cols = columns || [...new Set(rows.flatMap(r => Object.keys(r)))];
  const head = el("tr", {}, cols.map(c => el("th", {text: Array.isArray(c) ? c[1] : c})));
  if (actions) head.append(el("th", {text: ""}));
  const body = rows.map(r => {
    const tr = el("tr", {}, cols.map(c => {
      const k = Array.isArray(c) ? c[0] : c;
      const v = typeof k === "function" ? k(r) : r[k];
      return el("td", {}, v instanceof Node ? v : fmt(String(k), v));
    }));
    if (actions) tr.append(el("td", {class: "actions"}, actions(r)));
    return tr;
  });
  return el("table", {}, el("thead", {}, head), el("tbody", {}, body));
}

function card(title, ...kids) { return el("section", {class: "card"}, title ? el("h2", {text: title}) : null, kids); }
function hint(t) { return el("p", {class: "hint", text: t}); }
function field(label, input) { return el("label", {}, label, input); }
function input(name, props) { return el("input", Object.assign({name: name}, props || {})); }
function select(name, options, value) {
  return el("select", {name: name}, options.map(([v, t]) => el("option", {value: v, selected: v === value}, t)));
}
function button(text, onclick, cls) { return el("button", {type: "button", class: cls || "", onclick: onclick}, text); }

function flash(ok, message, detail) {
  const box = document.getElementById("flash");
  box.replaceChildren(el("div", {class: "message " + (ok ? "good" : "bad")},
    el("strong", {class: ok ? "ok" : "error", text: message}),
    detail !== undefined ? view(detail) : null));
  box.scrollIntoView({block: "nearest"});
}

function setView(...kids) {
  blobs.forEach(u => URL.revokeObjectURL(u));
  blobs = [];
  document.getElementById("view").replaceChildren(...kids.flat());
}

function formData(form) {
  const out = {};
  for (const e of form.elements) {
    if (!e.name) continue;
    if (e.type === "checkbox") out[e.name] = e.checked;
    else if (e.type === "number") out[e.name] = e.value === "" ? "" : Number(e.value);
    else out[e.name] = e.value.trim();
  }
  return out;
}

// ---- the server ----

class Locked extends Error {}

async function api(method, path, body) {
  const headers = {"X-Admin-Token": token()};
  const init = {method: method, headers: headers, credentials: "omit", cache: "no-store",
                redirect: "error", referrerPolicy: "no-referrer"};
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body || {});
  }
  const r = await fetch(API + path, init);
  if (r.status === 401) {
    sessionStorage.removeItem(KEY);
    showLogin("The token was wrong or is no longer the server's.");
    throw new Locked();
  }
  const type = r.headers.get("Content-Type") || "";
  if (!type.startsWith("application/json")) return {status: r.status, ok: r.ok, blob: await r.blob()};
  const data = await r.json();
  return {status: r.status, ok: r.ok, data: data};
}

async function get(path) {
  const r = await api("GET", path);
  if (!r.ok) {
    flash(false, "The server answered " + r.status, r.data);
    return null;
  }
  return r.data;
}

// A change: asks first when `ask` is given, shows the answer, then `after` (a reload).
async function act(path, body, ask, after) {
  if (ask && !window.confirm(ask)) return null;
  let r;
  try {
    r = await api("POST", path, body);
  } catch (e) {
    if (!(e instanceof Locked)) flash(false, "The request failed: " + e.message);
    return null;
  }
  flash(r.ok, r.ok ? "Done." : "The server refused it (" + r.status + ")", r.data);
  if (r.ok && after) await after();
  return r.ok ? r.data : null;
}

async function image(path, holder) {
  const r = await api("GET", path);
  if (!r.blob) {
    holder.replaceChildren(el("span", {class: "error", text: (r.data && r.data.error) || "no image"}));
    return;
  }
  const url = URL.createObjectURL(r.blob);
  blobs.push(url);
  holder.replaceChildren(el("img", {class: "thumb", src: url, alt: "preview"}));
}

function imageCell(path, auto) {
  const holder = el("div");
  const show = button("Show", () => image(path, holder), "small quiet");
  holder.append(show);
  if (auto) image(path, holder);
  return holder;
}

// ---- tabs ----

const RENDER = {};
let current = "status";

function go(tab, arg) {
  current = tab;
  for (const b of document.querySelectorAll("#tabs button")) {
    if (b.dataset.tab === tab) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  document.getElementById("flash").replaceChildren();
  setView(el("p", {class: "muted", text: "Loading..."}));
  RENDER[tab](arg).catch(e => {
    if (!(e instanceof Locked)) setView(card("Error", el("p", {class: "error", text: String(e)})));
  });
}

function playerLink(xuid, name) {
  return button(name || xuid, () => go("players", xuid), "small quiet");
}

// ---- Status ----

RENDER.status = async function () {
  const d = await get("overview");
  if (!d) return setView();
  panel = d.panel;
  const s = d.status;
  const players = Object.entries(s.rooms || {}).flatMap(([room, list]) => list.map(p => Object.assign({room: room}, p)));
  const worlds = Object.entries(s.worlds || {}).map(([id, w]) => ({session: id, host: w.host, humans: w.humans, event: w.event}));
  const stat = (n, label) => el("div", {}, el("div", {class: "stat", text: n}), el("div", {class: "muted", text: label}));
  const worldForm = el("form", {class: "row"},
    field("World", select("session", [["all", "every world"]].concat(worlds.map(w => [w.session, w.session + " (" + w.host + ")"])), "all")),
    field("Event id (0: random)", input("event", {type: "number", min: 0, value: 0})),
    el("button", {type: "submit"}, "Start the event"));
  worldForm.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(worldForm);
    act("world-event", f, "End the free roam of " + (f.session === "all" ? "every Horizon World" : "world " + f.session) + " for an event?", RENDER.status);
  });
  const tokens = (s.score_tokens || {});
  setView(
    card("Server", el("div", {class: "grid"},
      stat(s.players, "players online"), stat(worlds.length, "Horizon Worlds"),
      stat(duration(s.uptime), "up for"), stat(s.via_proxy || 0, "through the tunnel"),
      stat(s.relayed_packets, "packets relayed"), stat(s.dropped_packets, "packets over a rate limit")),
      el("p", {class: "hint"}, "Server id ", el("code", {text: panel.server_id}),
        " · score tokens: ", el("code", {text: panel.score_tokens}),
        " · challenges: ", panel.challenges ? "on" : "off",
        panel.accounts_admin ? [" · ", el("a", {href: panel.accounts_admin, target: "_blank", rel: "noopener noreferrer"}, "Website accounts admin")] : null)),
    card("Players online", table(players, ["gamertag", [p => playerLink(p.xuid, p.xuid), "xuid"], "room", [p => duration(p.online_for), "online for"], "via_proxy", "status"])),
    card("Horizon Worlds", table(worlds), el("h3", {text: "Start an event in a world (/world/event)"}),
      hint("The world's host ends its free roam and starts the event; Co-op Festival worlds are left out (their events are their host's)."), worldForm),
    card("Volunteer relays", s.relays ? table(s.relays) : el("p", {class: "muted", text: "No relay listed."})),
    card("Score tokens", view({mode: tokens.mode, open: tokens.open, checked: tokens.checked, failed: tokens.failed, no_start: tokens.no_start, too_fast: tokens.too_fast})),
    card("Signed requests", view(s.auth || {})),
    card("Friends and chat", view(Object.assign({}, s.friends || {}, {"players with chat": (s.chat || {}).with_chat, "chat messages kept": (s.chat || {}).kept}))),
    card("Discord status message" + (d.discord.enabled ? "" : " (preview: FORZA_DISCORD_STATUS_WEBHOOK is not set)"),
      hint("What the server would post now. Community servers show their last check by the status loop."),
      el("pre", {text: d.discord.text || d.discord.error || ""})),
    card("Raw status page", el("details", {}, el("summary", {text: "Show /status as JSON"}), el("pre", {text: JSON.stringify(s, null, 1)}))));
};

// ---- Messages ----

RENDER.messages = async function () {
  const d = await get("broadcasts");
  if (!d) return setView();
  const names = Object.fromEntries(d.types.map(t => [t.type, t.name]));
  const typeSelect = () => select("type", d.text_types.map(t => [t, t + " " + names[t]]), "0x0100");
  const bform = el("form", {class: "row"},
    field("Type", typeSelect()),
    field("Text (at most 512)", el("textarea", {name: "text", maxlength: 512, required: true})),
    field("Days", input("days", {type: "number", min: 1, max: d.max_days, value: 7})),
    el("button", {type: "submit"}, "Broadcast"));
  bform.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(bform);
    act("broadcasts", f, "Send this message to every player, new ones included, for " + f.days + " days?", RENDER.messages);
  });
  const nform = el("form", {class: "row"},
    field("To (gamertag or XUID)", input("to", {required: true, maxlength: 32})),
    field("Type", select("type", d.text_types.map(t => [t, t + " " + names[t]]).concat([["sample", "samples: one of each kind (a test)"]]), "0x0100")),
    field("Text (not for samples)", el("textarea", {name: "text", maxlength: 512})),
    el("button", {type: "submit"}, "Send"));
  nform.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(nform);
    act("notify", f, "Send this message to " + f.to + "?");
  });
  setView(
    card("Broadcast to everyone (/broadcast)", hint("One Message Center message for every player, stored once; a player who signs in later gets it too while it lasts."), bform),
    card("Live broadcasts", table(d.broadcasts, ["id", "type", "name", "text", "sent", "expires"], b => [
      button("Delete", () => act("broadcasts/delete", {id: b.id}, "Take broadcast " + b.id + " back from every Message Center?", RENDER.messages), "small danger")])),
    card("Message one player (/notify)", hint("A Message Center message for one player who signed in here."), nform));
};

// ---- Gifts ----

function giftForm(to, after) {
  const max = panel.gift_max || 1000000;
  const form = el("form", {class: "row"},
    field("To (gamertag or XUID)", input("to", {required: true, maxlength: 32, value: to || ""})),
    field("Gift", select("kind", [["credits", "credits"], ["car", "a stock car (Data_Car id)"]], "credits")),
    field("Credits (at most " + max.toLocaleString() + ") or car id", input("amount", {type: "number", min: 1, max: max, required: true})),
    field("Reason (the player sees it)", input("reason", {required: true, maxlength: 200, size: 40})),
    el("button", {type: "submit"}, "Send the gift"));
  form.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(form);
    const body = {to: f.to, reason: f.reason};
    body[f.kind === "car" ? "car" : "credits"] = f.amount;
    const what = f.kind === "car" ? "car " + f.amount : Number(f.amount).toLocaleString() + " credits";
    act("gifts", body, "Gift " + what + " to " + f.to + "?", after);
  });
  return form;
}

RENDER.gifts = async function () {
  if (!panel.server_id) await get("overview").then(d => { if (d) panel = d.panel; });
  const d = await get("gifts");
  if (!d) return setView();
  setView(
    card("Gift credits or a car (/gift)", hint("The player collects it from the Message Center. The panel caps a credits gift at " + d.gift_max.toLocaleString() + " (FORZA_PANEL_GIFT_MAX)."), giftForm("", RENDER.gifts)),
    card("Recent gifts", hint("Every gift on the server, newest first: the operator's, the weekly challenge gifts and players' car gifts."),
      table(d.gifts, ["id", "type", "credits", "car", [g => playerLink(g.to, g.gamertag || g.to), "to"], "sender", "sent", "retrieved"])));
};

// ---- Players ----

RENDER.players = async function (xuid) {
  const form = el("form", {class: "row"},
    field("Gamertag or XUID", input("q", {required: true, maxlength: 32, autofocus: true})),
    el("button", {type: "submit"}, "Search"));
  const results = el("div");
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const d = await get("players?q=" + encodeURIComponent(formData(form).q));
    if (d) results.replaceChildren(table(d.players, [[p => playerLink(p.xuid, p.gamertag || p.xuid), "gamertag"], "xuid", "seen", "online"]));
  });
  if (!xuid) return setView(card("Find a player", form, results));
  const p = await get("player?xuid=" + encodeURIComponent(xuid));
  if (!p) return setView(card("Find a player", form, results));
  const reload = () => go("players", p.xuid);
  const banForm = el("form", {class: "row"},
    field("Reason", input("reason", {maxlength: 200, size: 40, required: !p.bans.content})),
    el("button", {type: "submit", class: p.bans.content ? "" : "danger"}, p.bans.content ? "Lift the ban" : "Ban from the server"));
  banForm.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(banForm);
    act("bans", {xuid: p.xuid, ban: !p.bans.content, reason: f.reason},
      p.bans.content ? "Lift the ban of " + p.gamertag + "? Their scores, photos, designs and challenge progress are shown again." : "Ban " + p.gamertag + " from the server? They are dropped at once, cannot sign in or write anything, and their scores, photos, designs and challenge progress are hidden (not deleted) until the ban is lifted.", reload);
  });
  const timedForm = chatForm(p.xuid, reload);
  setView(
    card("Find a player", form, results),
    card(p.gamertag || p.xuid,
      p.banned ? el("p", {}, el("span", {class: "tag off", text: "Banned"}), " from this server: no sign-in, no writes, nothing of theirs listed.") : null,
      view({
      xuid: p.xuid, gamertag: p.gamertag, key: p.key.text + (p.key.public_key ? " (" + p.key.public_key + ")" : ""),
      "signed sign-ins": p.signed_logons.days + " days" + (p.signed_logons.first ? ", the first " + when(p.signed_logons.first) : ""),
      "first result on the boards": p.first_score ? when(p.first_score) : "none yet (the website link waits for one)",
      last_seen: p.last_seen, last_logon: p.last_logon, logons: p.logons, "career level": p.career_level,
      build: p.build || "", "builds on the boards": p.builds.join(", "),
      friends: p.friends.friends + " friends, " + p.friends.requests_in + " requests in, " + p.friends.requests_out + " out, " + p.friends.blocked + " blocked",
      club: p.club ? "[" + p.club.tag + "] " + p.club.name + " (role " + p.club.role + ")" : "none",
      online: p.online.length ? p.online.map(o => o.room + ", " + duration(o.online_for)).join("; ") : "no"}),
      p.accounts_admin ? el("p", {}, el("a", {href: p.accounts_admin, target: "_blank", rel: "noopener noreferrer"}, "Find this XUID in the website accounts admin")) : null),
    card("Bans", view({"banned": p.bans.content ? "yes" + (p.bans.content_reason ? ": " + p.bans.content_reason : "") : "no",
      "sign-in ban until": p.bans.signin_until, "chat mute until": p.bans.muted_until}), banForm,
      el("h3", {text: "Timed sign-in ban or chat mute"}), timedForm),
    card("Gift", giftForm(p.xuid, reload)),
    card("Scores (" + p.scores.ranked + " ranked, " + p.scores.unranked + " unranked)",
      scoreTable(p.scores.rows, reload)),
    card("Photos", photoTable(p.photos, reload, false)),
    card("Videos", videoTable(p.videos || [], reload, false)),
    card("Designs", designTable(p.designs, reload, false)),
    card("Challenges", p.challenges ? [view(p.challenges.player), p.challenges.sets.map(s => [
      el("h3", {text: s.kind + " (" + when(s.starts) + " to " + when(s.ends) + ")"}),
      table(s.objectives, ["title", "type", "target", "progress", "done"])])] : el("p", {class: "muted", text: "Challenges are off."})),
    card("Gifts received", table(p.gifts)));
};

// ---- Bans and chat ----

function chatForm(who, after) {
  const form = el("form", {class: "row"},
    field("Gamertag or XUID", input("who", {required: true, maxlength: 32, value: who || ""})),
    field("What", select("kind", [["kick", "sign-in ban (kick)"], ["mute", "chat mute"]], "kick")),
    field("Minutes (0 lifts it)", input("minutes", {type: "number", min: 0, max: 525600, value: 60})),
    field("Reason", input("reason", {maxlength: 200, size: 30})),
    el("button", {type: "submit", class: "danger"}, "Apply"));
  form.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(form);
    const what = f.kind === "kick" ? "Drop " + f.who + " and refuse their sign-in" : "Mute " + f.who + "'s chat";
    act("chat/" + f.kind, {who: f.who, minutes: f.minutes, reason: f.reason},
      f.minutes === 0 ? "Lift it for " + f.who + "?" : what + " for " + f.minutes + " minutes?", after);
  });
  return form;
}

RENDER.bans = async function () {
  const [b, chat] = await Promise.all([get("bans"), get("chat")]);
  if (!b || !chat) return setView();
  const banForm = el("form", {class: "row"},
    field("XUID (16 hex digits)", input("xuid", {required: true, pattern: "(0x)?[0-9A-Fa-f]{16}", size: 20})),
    field("Reason", input("reason", {required: true, maxlength: 200, size: 40})),
    el("button", {type: "submit", class: "danger"}, "Ban"));
  banForm.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(banForm);
    act("bans", {xuid: f.xuid, ban: true, reason: f.reason}, "Ban " + f.xuid + "?", RENDER.bans);
  });
  setView(
    card("Bans (banned_xuids.json)", hint("A banned player is dropped at once and cannot sign in. Nothing they send is taken (scores, ghosts, stats, challenges, co-op results, chat, friend requests, auctions, gifts, photos, designs), and their scores, photos, designs, challenge progress and auctions are listed to nobody. Nothing is deleted: lifting the ban shows it all again."),
      table(b.content, [[r => playerLink(r.xuid, r.xuid), "xuid"], "gamertag", "reason"], r => [
        button("Lift", () => act("bans", {xuid: r.xuid, ban: false}, "Lift the ban of " + r.xuid + "?", RENDER.bans), "small")]),
      banForm),
    card("Sign-in bans and chat mutes (moderation.json)", hint("A sign-in ban drops the player and refuses their sign-in until it runs out (the game shows \"removed by the server admin\")."),
      table(b.timed, ["kind", [r => playerLink(r.xuid, r.gamertag), "player"], "until", [r => duration(r.left), "left"]], r => [
        button("Lift", () => act("chat/" + (r.kind === "mute" ? "mute" : "kick"), {who: r.xuid, minutes: 0}, "Lift it for " + r.gamertag + "?", RENDER.bans), "small")]),
      chatForm("", RENDER.bans)),
    card("Recent chat (/admin/chat, in memory only)", table(chat.messages.slice().reverse(), ["time", "from", [m => playerLink(m.xuid, m.xuid), "xuid"], "channel", "club", [m => (m.to || []).join(", "), "to"], "text"])));
};

// ---- Scores ----

function scoreTable(rows, after) {
  return table(rows, ["state", "board", [r => playerLink(r.xuid, r.gamertag || r.xuid), "player"], "score", "carId", "classId", "submitted", "server", "build", "verified", "unranked", "ghost"], r => {
    const filters = {player: r.xuid, board: r.board, state: r.state};
    return r.state === "ranked"
      ? [button("Unrank", () => act("scores/unrank", {filters: filters}, "Take this row off its board (kept for review)?", after), "small"),
         button("Delete", () => act("scores/delete", {filters: filters}, "Delete this row and its ghost?", after), "small danger")]
      : [button("Rank", () => act("scores/rank", {filters: filters}, "Put this row back on its board (where it beats the player's row there)?", after), "small"),
         button("Delete", () => act("scores/delete", {filters: filters}, "Delete this row and its kept ghost?", after), "small danger")];
  });
}

RENDER.scores = async function (preset) {
  const filters = preset || {state: "all"};
  const qs = Object.entries(filters).filter(([, v]) => v !== "" && v !== undefined).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
  const d = await get("scores" + (qs ? "?" + qs : ""));
  if (!d) return setView();
  const form = el("form", {class: "row"},
    field("State", select("state", [["all", "all"], ["ranked", "ranked"], ["unranked", "unranked"]], filters.state || "all")),
    field("Build (none: before tags)", input("build", {value: filters.build || "", size: 14})),
    field("Server id", input("server", {value: filters.server || "", size: 10})),
    field("Verified", select("verified", [["", "any"], ["1", "yes"], ["0", "no"]], filters.verified || "")),
    field("Player XUID", input("player", {value: filters.player || "", size: 18})),
    field("Board (3:199:0 or event-199)", input("board", {value: filters.board || "", size: 12})),
    field("Reason contains", input("reason", {value: filters.reason || "", size: 12})),
    el("button", {type: "submit"}, "Find"));
  form.addEventListener("submit", ev => { ev.preventDefault(); go("scores", formData(form)); });
  const change = filters2 => {
    const f = Object.fromEntries(Object.entries(filters2).filter(([, v]) => v !== ""));
    return f;
  };
  const f = change(filters);
  const bulk = ["unrank", "rank", "delete"].map(a => button(a[0].toUpperCase() + a.slice(1) + " all " + d.matched + " matching",
    () => act("scores/" + a, {filters: f}, a + " every one of the " + d.matched + " matching rows? (" + JSON.stringify(f) + ")", () => go("scores", filters)), a === "delete" ? "danger" : ""));
  const t = d.score_tokens || {};
  const boards = Object.entries(t.boards || {}).map(([k, v]) => Object.assign({board: k}, v));
  setView(
    card("Leaderboard rows (/admin/scores)", hint("A change needs a filter besides the state. Unrank keeps rows (and ghosts) for review; rank puts unranked rows back where they beat the player's row."),
      form, el("div", {class: "row"},
        button("Held ghosts", () => go("scores", {state: "unranked", reason: "ghost"}), "quiet"),
        button("Failed start checks", () => go("scores", {state: "unranked", reason: "start"}), "quiet"),
        button("Unverified", () => go("scores", {state: "unranked", reason: "unverified"}), "quiet"),
        button("Rows before tags", () => go("scores", {state: "all", build: "none"}), "quiet")),
      el("p", {}, d.matched + " rows match (at most 500 shown)."),
      Object.keys(f).some(k => k !== "state") ? el("div", {class: "row"}, bulk) : hint("Add a filter to change the matching rows at once."),
      scoreTable(d.rows, () => go("scores", filters))),
    card("Score tokens (--score-tokens " + (t.mode || "") + ")", hint("Mode is set at start (--score-tokens or FORZA_SCORE_TOKENS): log, enforce or off. Counts are since the server started."),
      view({open: t.open, checked: t.checked, failed: t.failed, no_start: t.no_start, too_fast: t.too_fast}), table(boards)));
};

// ---- Photos ----

function photoTable(photos, after, auto) {
  return table(photos, [[p => imageCell("photos/img/" + p.image.split("/").pop(), auto), "picture"], "id", [p => playerLink(p.owner, p.gamertag), "owner"], "title", "status", "banned", "public", "note", "takenAt", "decidedAt"], p => {
    const a = (action, ask, cls) => button(action, () => act("photos/" + p.id + "/" + action, action === "ban" ? {reason: window.prompt("Reason for the ban:") || ""} : {note: "operator panel"}, ask, after), "small " + (cls || ""));
    return [
      p.status === "held" ? a("release", null) : null,
      p.status === "live" ? a("hold", null) : null,
      p.status !== "hidden" ? a("hide", "Hide photo " + p.id + " (out of the feed and the image route)?") : a("unhide", null),
      a("delete", "Delete photo " + p.id + " for good? The same picture is not published again.", "danger"),
      p.banned ? null : a("ban", "Ban the owner " + p.gamertag + " (" + p.owner + ")?", "danger")];
  });
}

RENDER.photos = async function (which) {
  which = which || "queue";
  const d = await get(which === "queue" ? "photos/queue" : "photos" + (which === "all" ? "" : "?status=" + which));
  if (!d) return setView();
  const pick = select("which", [["queue", "the queue (held, oldest first)"], ["live", "live"], ["held", "held"], ["hidden", "hidden"], ["all", "all"]], which);
  pick.addEventListener("change", () => go("photos", pick.value));
  const shown = d.photos.slice(0, 200);
  setView(card("Community photos", hint("Release publishes a held photo; hold takes a live one back to the queue; hide takes it out of the feed and the image route; delete removes it. A photo the operator hid cannot be released: unhide it."),
    el("div", {class: "row"}, field("Show", pick)),
    el("p", {}, d.photos.length + " photos" + (d.photos.length > shown.length ? " (the first 200 shown)" : "") + "."),
    photoTable(shown, () => go("photos", which), which === "queue")));
};

// ---- Videos ----

async function play(path, holder) {
  const r = await api("GET", path);
  if (!r.blob) {
    holder.replaceChildren(el("span", {class: "error", text: (r.data && r.data.error) || "no video"}));
    return;
  }
  const url = URL.createObjectURL(r.blob);
  blobs.push(url);
  holder.replaceChildren(el("video", {class: "thumb", src: url, controls: true, preload: "metadata"}));
}

function videoCell(v, auto) {
  if (!v.poster) return el("span", {class: "muted", text: "converting"});
  const holder = el("div");
  holder.append(imageCell("videos/media/" + v.id + ".jpg", auto),
    button("Play", () => play("videos/media/" + v.id + ".mp4", holder), "small quiet"),
    button("Frames", () => {
      const frames = el("div", {class: "row"});
      holder.append(frames);
      v.frames.forEach(f => { const h = el("div"); frames.append(h); image("videos/frame/" + f.split("/").pop(), h); });
    }, "small quiet"));
  return holder;
}

function videoTable(list, after, auto) {
  return table(list, [[v => videoCell(v, auto), "video"], "id", [v => playerLink(v.owner, v.gamertag), "owner"], "title", "status", [v => v.seconds + " s", "length"], "banned", "public", "note", "takenAt", "decidedAt"], v => {
    const a = (action, ask, cls) => button(action, () => act("videos/" + v.id + "/" + action, action === "ban" ? {reason: window.prompt("Reason for the ban:") || ""} : {note: "operator panel"}, ask, after), "small " + (cls || ""));
    return [
      v.status === "held" ? a("release", null) : null,
      v.status === "live" ? a("hold", null) : null,
      v.status === "hidden" ? a("unhide", null) : v.status !== "converting" ? a("hide", "Hide video " + v.id + " (out of the feed and its routes)?") : null,
      a("delete", "Delete video " + v.id + " for good? The same clip is not published again.", "danger"),
      v.banned ? null : a("ban", "Ban the owner " + v.gamertag + " (" + v.owner + ")?", "danger")];
  });
}

RENDER.videos = async function (which) {
  which = which || "queue";
  const d = await get(which === "queue" ? "videos/queue" : "videos" + (which === "all" ? "" : "?status=" + which));
  if (!d) return setView();
  const pick = select("which", [["queue", "the queue (held, oldest first)"], ["converting", "converting"], ["live", "live"], ["held", "held"], ["hidden", "hidden"], ["all", "all"]], which);
  pick.addEventListener("change", () => go("videos", pick.value));
  const shown = d.videos.slice(0, 200);
  setView(card("Community videos", hint("The game's movie uploads, converted by the video converter. Every video waits held until the screening worker or you release it. Release publishes a held video; hold takes a live one back to the queue; hide takes it out of the feed and its routes; delete removes it. Frames are the pictures the screening scored."),
    el("div", {class: "row"}, field("Show", pick)),
    el("p", {}, d.videos.length + " videos" + (d.videos.length > shown.length ? " (the first 200 shown)" : "") + "."),
    videoTable(shown, () => go("videos", which), which === "queue")));
};

// ---- Designs ----

function designTable(list, after, auto) {
  return table(list, [[x => imageCell("designs/img/" + x.id + ".png", auto), "preview"], "id", "type", "code", "title", [x => x.owner ? playerLink(x.owner, x.gamertag) : x.gamertag, "owner"], "car", "state", "listed", "reports", "note", "createdAt"], x => {
    const a = (action, ask, cls) => button(action, () => act("designs/" + x.id + "/" + action, action === "ban" ? {reason: window.prompt("Reason for the ban:") || ""} : {note: "operator panel"}, ask, after), "small " + (cls || ""));
    return [
      x.state === "pending" || x.state === "held" ? a("release", null) : null,
      x.state === "pending" || x.state === "clear" ? a("hold", "Take design " + x.id + " off the website (it stays in the game)?") : null,
      x.state !== "hidden" ? a("hide", "Hide design " + x.id + " from the website and the game?") : a("unhide", null),
      a("delete", "Delete design " + x.id + " from the game and the website?", "danger"),
      x.banned ? null : a("ban", "Ban the owner of design " + x.id + "?", "danger")];
  });
}

RENDER.designs = async function (which) {
  which = which || "queue";
  const d = await get(which === "queue" ? "designs/queue" : "designs" + (which === "all" ? "" : "?state=" + which));
  if (!d) return setView();
  const pick = select("which", [["queue", "the queue (pending, oldest first)"], ["pending", "pending"], ["clear", "clear"], ["held", "held"], ["hidden", "hidden"], ["all", "all"]], which);
  pick.addEventListener("change", () => go("designs", pick.value));
  const shown = d.designs.slice(0, 200);
  setView(card("Community designs", hint("Release puts a design on the website; hold takes it off the website (still in the game); hide takes it off both; delete removes it from the game too. A hidden design cannot be released: unhide it."),
    el("div", {class: "row"}, field("Show", pick)),
    el("p", {}, (d.pending !== undefined ? d.pending + " pending, " : "") + d.designs.length + " shown here" + (d.designs.length > shown.length ? " (the first 200)" : "") + "."),
    designTable(shown, () => go("designs", which), which === "queue")));
};

// ---- Servers ----

function entryForm(slot, entry, own, after) {
  entry = entry || {};
  const form = el("form", {class: "row"},
    field("Name (48)", input("name", {required: true, maxlength: 48, value: entry.name || ""})),
    field("Address (host[:port], the UDP port)", input("address", {required: true, maxlength: 261, value: entry.address || "", size: 26})),
    field("Region (32)", input("region", {maxlength: 32, value: entry.region || ""})),
    field("Contact (120)", input("contact", {maxlength: 120, value: entry.contact || "", size: 24})),
    own ? null : field("Server id", input("id", {maxlength: 64, value: entry.id || "", size: 12})),
    own ? null : el("label", {class: "check"}, el("input", {type: "checkbox", name: "verified", checked: !!entry.verified}), " verified"),
    el("button", {type: "submit"}, slot === "new" ? "Add" : "Save"));
  form.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(form);
    act("servers/save", {slot: slot, before: slot === "new" ? null : (entry.address || null), entry: f},
      slot === "new" ? "Add " + f.name + " to the launcher's server list?" : "Save the changes to " + (entry.name || "this entry") + "?", after);
  });
  return form;
}

async function check(address, cell) {
  cell.replaceChildren(el("span", {class: "muted", text: "checking..."}));
  const d = await act("servers/check", {address: address});
  if (d) cell.replaceChildren(statusTag(d.checks[0]));
}

function statusTag(c) {
  return c.up ? el("span", {class: "tag on", text: "up, " + c.players + " players, " + c.ms + " ms"}) : el("span", {class: "tag off", text: "no /status answer"});
}

RENDER.servers = async function () {
  const d = await get("servers");
  if (!d) return setView();
  const edit = el("div");
  const rows = d.servers.map((s, i) => Object.assign({index: i}, s));
  setView(
    card("The launcher's server list (server_list.json)", hint("Players' launchers take the list from /servers.json, signed with this server's list key. The server reads the file again when it changes. The launcher pings http://host:<port + 1>/status of each entry: check it answers before listing a server."),
      view({file: d.file ? "server_list.json" : "none yet (saving an entry makes it)", "public key": d.public_key || "none yet (made at the first /servers.json request)", "this server's id": d.server_id}),
      d.problems.length ? [el("h3", {class: "error", text: "Left out of the list"}), el("ul", {}, d.problems.map(p => el("li", {text: p})))] : null,
      el("div", {class: "row"}, button("Check every address", async () => {
        const r = await act("servers/check", {});
        if (r) flash(true, "Checked", r.checks);
      }))),
    card("This server (self)", d.self ? view(d.self) : el("p", {class: "muted", text: "Not set."}),
      el("details", {}, el("summary", {text: d.self ? "Edit" : "Set"}), entryForm("self", d.self, true, RENDER.servers))),
    card("Community servers", table(rows, ["index", "name", "address", "region", "contact", "verified", "id", [r => {
      const cell = el("span");
      cell.append(button("Check", () => check(r.address, cell), "small quiet"));
      return cell;
    }, "status"]], r => [
      button("Edit", () => { edit.replaceChildren(el("h3", {text: "Edit " + r.name}), entryForm(r.index, r, false, RENDER.servers)); edit.scrollIntoView(); }, "small"),
      button("Remove", () => act("servers/remove", {slot: r.index, before: r.address}, "Take " + r.name + " (" + r.address + ") off the list?", RENDER.servers), "small danger")]),
      edit, el("h3", {text: "Add a server"}), entryForm("new", null, false, RENDER.servers)));
};

// ---- Relays ----

RENDER.relays = async function () {
  const d = await get("relays");
  if (!d) return setView();
  const live = Object.fromEntries((d.status.relays || []).map(r => [r.id, r]));
  const rows = d.entries.map(e => Object.assign({}, e, live[e.id] || {}));
  const form = el("form", {class: "row"},
    field("Id (a-z, 0-9, -)", input("id", {required: true, pattern: "[a-z0-9][a-z0-9-]{0,31}", size: 10})),
    field("Name", input("name", {required: true, maxlength: 48})),
    field("Address (host:port, UDP)", input("address", {required: true, maxlength: 261, size: 24})),
    field("Region", input("region", {maxlength: 32})),
    field("Contact", input("contact", {maxlength: 120})),
    el("button", {type: "submit"}, "Add"));
  form.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = formData(form);
    const again = d.entries.some(e => e.id === f.id);
    act("relays/add", f, again ? f.id + " is listed: give it a new secret? The old one stops working." : "Add relay " + f.id + "? Check the volunteer first: a relay sees the players' IP addresses.", RENDER.relays);
  });
  setView(card("Volunteer relays (relay_list.json)", hint("Adding a relay writes its entry and a new secret to relay_secrets/<id>.txt in the data folder: send that file to the relay's operator privately (the panel never shows it). Removing one revokes it."),
    d.problems.length ? el("ul", {}, d.problems.map(p => el("li", {class: "error", text: p}))) : null,
    table(rows, ["id", "name", "address", "region", "contact", "up", "linked", "listed", "probation", "probe_ms", "probe_fails", "sessions", "players", "capacity"], r => [
      button("Remove", () => act("relays/remove", {id: r.id}, "Revoke relay " + r.id + "? Its secret stops working and its players go back to this server's relay.", RENDER.relays), "small danger")]),
    el("h3", {text: "Add a relay, or give a listed one a new secret"}), form));
};

// ---- Challenges ----

RENDER.challenges = async function () {
  const d = await get("challenges");
  if (!d) return setView();
  if (!d.enabled) return setView(card("Daily and weekly challenges", el("p", {class: "muted", text: "Off on this server (--daily-challenges)."})));
  setView(
    card("Daily and weekly challenges", view({"weekly gift": d.gifts ? d.gift_credits.toLocaleString() + " credits after " + d.gift_after + " objectives" : "off", week: d.week, "players this week": d.week_players}),
      d.sets.map(s => [el("h3", {text: (s.kind === "daily" ? "Today" : "This week") + " (" + when(s.starts) + " to " + when(s.ends) + ")"}),
        table(s.objectives, ["title", "type", "target", ["completed", "players who finished"]])])),
    card("Gifts sent this week", table(d.gifts_sent, [[g => playerLink(g.xuid, g.gamertag || g.xuid), "player"], "finished", "sent", "gift"])),
    card("Progressing this week, no gift yet", table(d.almost, [[g => playerLink(g.xuid, g.gamertag || g.xuid), "player"], "finished"])));
};

// ---- Events ----

const EVENT_TEMPLATE = {id: "festival-night", name: "Festival Night", description: "Meet at the festival for a community cruise.",
  activity: "Online free roam", session: "0123456789abcdef", starts: 0, ends: 0, organizer: "Festival organizer", rewards: [1197]};

RENDER.events = async function () {
  const d = await get("events");
  if (!d) return setView();
  const now = Math.floor(Date.now() / 1000);
  const create = el("textarea", {class: "json", name: "event"});
  create.value = JSON.stringify(Object.assign({}, EVENT_TEMPLATE, {starts: now + 600, ends: now + 4200}), null, 2);
  const createForm = el("form", {}, field("The event's JSON (starts and ends are Unix seconds, at most seven days apart; session: a real id from the status page)", create), el("button", {type: "submit"}, "Create"));
  createForm.addEventListener("submit", ev => {
    ev.preventDefault();
    let event;
    try { event = JSON.parse(create.value); } catch (e) { return flash(false, "That is not JSON: " + e.message); }
    act("events", {event: event}, "Create event " + event.id + "?", RENDER.events);
  });
  const enroll = el("form", {class: "row"}, field("Signed-in player's XUID", input("xuid", {required: true, pattern: "(0x)?[0-9A-Fa-f]{16}"})), el("button", {type: "submit"}, "Enroll"));
  enroll.addEventListener("submit", ev => { ev.preventDefault(); const f = formData(enroll); act("events/enroll", f, "Enroll " + f.xuid + "'s current signed credential?", RENDER.events); });
  const detail = el("div");
  const showAttendance = async e => {
    const a = await get("events/" + encodeURIComponent(e.id) + "/attendance");
    if (!a) return;
    const award = el("form", {class: "row"},
      field("XUID", input("xuid", {required: true, pattern: "(0x)?[0-9A-Fa-f]{16}"})),
      field("Reward ids already owned (comma separated; empty: none)", input("owned", {size: 20})),
      el("button", {type: "submit"}, "Award"));
    award.addEventListener("submit", ev => {
      ev.preventDefault();
      const f = formData(award);
      const owned = f.owned ? f.owned.split(",").map(s => Number(s.trim())) : [];
      act("events/" + encodeURIComponent(e.id) + "/award", {xuid: f.xuid, owned_models: owned}, "Award " + e.id + "'s rewards to " + f.xuid + "?", () => showAttendance(e));
    });
    detail.replaceChildren(card("Attendance: " + e.id,
      table(a.attendees, [[r => playerLink(r.xuid, r.xuid), "xuid"], "first", "last", [r => duration(r.seconds), "time"]], r => [
        button("Revoke", () => act("events/" + encodeURIComponent(e.id) + "/revoke", {xuid: r.xuid}, "Revoke " + e.id + "'s grants for " + r.xuid + "?", () => showAttendance(e)), "small danger")]),
      el("h3", {text: "Award the event's rewards"}), award));
    detail.scrollIntoView();
  };
  setView(
    card("Community events (/admin/events, tools/event_admin.py)", hint("Enroll players, create an event, start it, review attendance and award its reward cars (docs/server_events.md)."),
      view({revision: d.revision, "event unlocks required (--event-unlocks)": d.required}),
      table(d.events, ["id", "name", "state", "session", "starts", "ends", "rewards", "organizer"], e => [
        e.state === "draft" ? button("Start", () => act("events/" + encodeURIComponent(e.id) + "/start", {}, "Start " + e.id + "?", RENDER.events), "small") : null,
        e.state !== "closed" ? button("Close", () => act("events/" + encodeURIComponent(e.id) + "/close", {}, "Close " + e.id + "? (awards stay valid)", RENDER.events), "small danger") : null,
        button("Attendance", () => showAttendance(e), "small quiet")])),
    detail,
    card("Enroll a player", hint("The player must be signed in now with a verified credential (their XUID is on the status page)."), enroll),
    card("Create an event", createForm));
};

// ---- Audit ----

RENDER.audit = async function () {
  const d = await get("audit?limit=500");
  if (!d) return setView();
  setView(card("Audit log (operator_audit.jsonl in the data folder)", hint("Every change made from this panel, newest first."),
    table(d.entries, ["at", "who", "from", "action", "target", "status", [e => JSON.stringify(e.detail), "detail"]])));
};

// ---- start ----

function showLogin(message) {
  document.getElementById("login").hidden = false;
  document.getElementById("forget").hidden = true;
  document.getElementById("tabs").replaceChildren();
  document.getElementById("login-message").textContent = message || "";
  setView();
  document.getElementById("token").focus();
}

function start() {
  document.getElementById("login").hidden = true;
  document.getElementById("forget").hidden = false;
  document.getElementById("tabs").replaceChildren(...TABS.map(([id, label]) => {
    const b = button(label, () => go(id));
    b.dataset.tab = id;
    return b;
  }));
  go(current);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("login-form").addEventListener("submit", ev => {
    ev.preventDefault();
    const t = document.getElementById("token");
    sessionStorage.setItem(KEY, t.value.trim());
    t.value = "";
    start();
  });
  document.getElementById("forget").addEventListener("click", () => {
    sessionStorage.removeItem(KEY);
    showLogin("The token is forgotten.");
  });
  if (token()) start(); else showLogin();
});
