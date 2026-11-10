// Operator console front end. All state comes from the adapter (console/server.py);
// this file only renders it and posts operator actions. Verdicts are computed server-side.
"use strict";

const SWITCH = "CommandRehearsal.payloadSwitch.";
const ui = {
  snap: null,          // last snapshot from /api/stream
  config: null,        // scenarios + command labels from /api/config
  adapter: "connecting", // connecting | online | offline
  offset: 0,           // server clock - browser clock, seconds
  open: new Set(),     // expanded timeline entry ids
  posting: false,      // an HTTP request from this page is in progress
  notice: null,        // {kind, text} shown in the command panel after a refused request
  mode: "SAFE",        // Set mode selection
  rendered: {},        // last HTML per section, to skip no-op re-renders
  autoOpen: null,      // timeline entry opened automatically when it was sent
  lastStepKey: "",     // rehearsal position last scrolled into view
};

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const shortName = (n) => String(n || "").split(".").pop();
const now = () => Date.now() / 1000 + ui.offset;
const pad = (n, w = 2) => String(n).padStart(w, "0");
function clock(epoch) {
  if (epoch == null) return "—";
  const d = new Date(epoch * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}
function fswClock(t) {
  const m = /T(\d\d:\d\d:\d\d(\.\d{1,3})?)/.exec(t || "");
  return m ? m[1] : (t || "—");
}
function age(epoch) {
  if (epoch == null) return "";
  const s = Math.max(0, now() - epoch);
  if (s < 10) return `${s.toFixed(1)} s ago`;
  if (s < 120) return `${Math.round(s)} s ago`;
  return `${Math.round(s / 60)} min ago`;
}
const linkUp = () => ui.adapter === "online" && ui.snap && ui.snap.link === "connected";
const recordsById = () => Object.fromEntries((ui.snap?.timeline || []).map((r) => [r.id, r]));

function setHTML(id, html) {
  if (ui.rendered[id] === html) return;
  const el = document.getElementById(id);
  const focusKey = el.contains(document.activeElement) ? document.activeElement.dataset.key : null;
  el.innerHTML = html;
  ui.rendered[id] = html;
  if (focusKey) el.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
}

// ------------------------------------------------------------------ outcome vocabulary
function responseChip(rec) {
  if (rec.outcome === "send_failed") return `<span class="chip chip--rej"><span class="chip__k">Response</span> Not sent</span>`;
  if (!rec.response) {
    return rec.phase === "done"
      ? `<span class="chip chip--warn"><span class="chip__k">Response</span> None</span>`
      : `<span class="chip chip--wait"><span class="chip__k">Response</span> Waiting</span>`;
  }
  return rec.response.status === "OK"
    ? `<span class="chip chip--ok"><span class="chip__k">Response</span> Accepted</span>`
    : `<span class="chip chip--rej"><span class="chip__k">Response</span> Rejected</span>`;
}
function evidenceChip(rec) {
  const map = {
    confirmed: ["ok", "Confirmed"],
    unconfirmed: ["warn", "Unconfirmed"],
    mismatch: ["rej", "Did not match"],
    send_failed: ["none", "No evidence"],
  };
  const [cls, text] = map[rec.outcome] || ["wait", rec.response ? "Waiting for telemetry" : "Waiting"];
  return `<span class="chip chip--${cls}"><span class="chip__k">Evidence</span> ${text}</span>`;
}
function originText(rec) {
  if (!rec.origin) return "Manual";
  const sc = ui.config?.scenarios.find((s) => s.id === rec.origin.scenario);
  return sc ? `${sc.title} · step ${rec.origin.step + 1}` : "Rehearsal";
}

// ------------------------------------------------------------------ header + strip
function renderLink() {
  const s = ui.snap;
  let cls = "", label = "Waiting for GDS", detail = "No downlink received yet";
  if (ui.adapter === "offline") {
    cls = "link--warn"; label = "Console adapter offline"; detail = "Reconnecting to 127.0.0.1…";
  } else if (ui.adapter === "connecting" || !s) {
    label = "Connecting…"; detail = "Console adapter";
  } else if (s.link === "connected") {
    cls = "link--ok"; label = "GDS connected"; detail = `Downlink <span data-age="${s.last_packet_at}">${age(s.last_packet_at)}</span>`;
  } else if (s.link === "lost") {
    cls = "link--warn"; label = "GDS link lost";
    detail = `No downlink for <span data-age="${s.last_packet_at}" data-bare="1">${age(s.last_packet_at).replace(" ago", "")}</span>`;
  }
  const el = document.getElementById("link");
  el.className = `link ${cls}`;
  setHTML("link", `<span class="link__dot" aria-hidden="true"></span>
    <span><span class="link__label">${label}</span><br><span class="link__detail">${detail}</span></span>`);
}

function staleNote(sample, channel) {
  if (!sample) return `<div class="reading__note">No ${channel} telemetry yet. It reports after a command.</div>`;
  if (!sample.fresh || !linkUp()) {
    return `<div class="reading__note"><span class="stale-tag">Last known</span> before link loss · FSW <span class="mono">${esc(fswClock(sample.fsw_time))}</span></div>`;
  }
  return `<div class="reading__note"><span class="mono">${channel}</span> · FSW <span class="mono">${esc(fswClock(sample.fsw_time))}</span> · received <span data-age="${sample.ground_time}">${age(sample.ground_time)}</span></div>`;
}

function renderStrip() {
  const t = ui.snap?.telemetry || {};
  const mode = t.mode, power = t.power;
  const stale = (x) => (x && (!x.fresh || !linkUp()) ? " reading--stale" : "");

  const modeVal = mode
    ? `<span class="reading__big modeplate modeplate--${mode.value === "SAFE" ? "safe" : "normal"}">${esc(mode.value)}</span>`
    : `<span class="unknown">Unknown</span>`;
  const powerVal = power
    ? `<span class="lamp ${power.value === "ON" ? "lamp--on" : ""}" aria-hidden="true"></span>
       <span class="reading__big ${power.value === "ON" ? "" : "power--off"}">${esc(power.value)}</span>`
    : `<span class="unknown">Unknown</span>`;

  const last = ui.snap?.last_tlm;
  let lastVal = `<span class="unknown">Unknown</span>`;
  let lastNote = `<div class="reading__note">No telemetry received yet.</div>`;
  if (last) {
    lastVal = `<span class="reading__big">${esc(fswClock(last.fsw_time))}</span>`;
    lastNote = linkUp()
      ? `<div class="reading__note">FSW time · received <span data-age="${last.ground_time}">${age(last.ground_time)}</span></div>`
      : `<div class="reading__note"><span class="stale-tag">Last known</span> received <span data-age="${last.ground_time}">${age(last.ground_time)}</span></div>`;
  }

  setHTML("strip", `
    <div class="reading${stale(mode)}"><div class="reading__label">Flight mode</div>
      <div class="reading__value">${modeVal}</div>${staleNote(mode, "PayloadMode")}</div>
    <div class="reading${stale(power)}"><div class="reading__label">Payload power</div>
      <div class="reading__value">${powerVal}</div>${staleNote(power, "PayloadSwitch")}</div>
    <div class="reading${last && !linkUp() ? " reading--stale" : ""}"><div class="reading__label">Last telemetry</div>
      <div class="reading__value">${lastVal}</div>${lastNote}</div>`);
}

// ------------------------------------------------------------------ command panel
function commandBlocker() {
  if (ui.adapter !== "online") return "Commands disabled: console adapter offline.";
  if (!ui.snap) return "Connecting…";
  if (ui.snap.link === "waiting") return "Commands disabled: waiting for the GDS link.";
  if (ui.snap.link === "lost") return "Commands disabled: GDS link lost.";
  return null;
}

function renderCommands() {
  const blocker = commandBlocker();
  const recs = recordsById();
  const flying = ui.snap?.in_flight ? recs[ui.snap.in_flight] : null;
  const disabled = blocker || flying || ui.posting ? "disabled" : "";

  let cls = "", label = "Ready", text = "One command at a time. Each waits for its response and telemetry.";
  if (blocker) { cls = "cmd-status--off"; label = "Unavailable"; text = blocker; }
  else if (flying) { cls = "cmd-status--busy"; label = `In flight · ${flying.label}`; text = flying.summary; }
  else if (ui.notice) { cls = `cmd-status--${ui.notice.kind}`; label = "Not sent"; text = ui.notice.text; }
  else {
    const lastCmd = (ui.snap?.timeline || []).find((r) => r.type === "command");
    if (lastCmd) { label = `Last · ${lastCmd.label}`; text = lastCmd.summary; }
  }

  const modeRadio = (m) => `<input type="radio" name="mode" id="mode-${m}" value="${m}" data-key="mode-${m}" ${ui.mode === m ? "checked" : ""} ${disabled}>
      <label for="mode-${m}">${m}</label>`;

  setHTML("commands", `<div class="panel__body">
    <div class="cmd-status ${cls}" role="status" aria-live="polite">
      <div class="cmd-status__label">${esc(label)}</div>
      <div class="cmd-status__text">${esc(text)}</div>
    </div>
    <div class="cmd-grid">
      <button class="btn" data-key="POWER_ON" data-cmd="POWER_ON" ${disabled}>Power on<span class="mono">POWER_ON</span></button>
      <button class="btn" data-key="POWER_OFF" data-cmd="POWER_OFF" ${disabled}>Power off<span class="mono">POWER_OFF</span></button>
    </div>
    <div class="setmode">
      <span class="setmode__label" id="setmode-l">Set mode <span class="mono faint">SET_MODE(newMode)</span></span>
      <div class="setmode__row">
        <div class="seg" role="radiogroup" aria-labelledby="setmode-l">${modeRadio("NORMAL")}${modeRadio("SAFE")}</div>
        <button class="btn" data-key="SET_MODE" data-cmd="SET_MODE" ${disabled}>Set mode</button>
      </div>
    </div>
    <p class="note">F´ enforces the SAFE interlock. Power on is sent in every mode so you can see the
      flight software's actual answer. Entering SAFE does not turn off a powered payload. Power off is always allowed.</p>
  </div>`);
}

// ------------------------------------------------------------------ rehearsal panel
function condState(target, sample) {
  if (!sample) return `<span class="chip chip--none">Unknown</span>`;
  if (!sample.fresh || !linkUp()) return `<span class="chip chip--warn">Last known ${esc(sample.value)}</span>`;
  return sample.value === target
    ? `<span class="chip chip--ok">Met</span>`
    : `<span class="chip chip--none">Now ${esc(sample.value)}</span>`;
}

function stepStatus(i, run, recs) {
  const ids = run.attempts[String(i)] || [];
  const rec = ids.length ? recs[ids[ids.length - 1]] : null;
  if (!rec) return { rec: null, chip: `<span class="chip chip--none">Not sent</span>` };
  if (rec.outcome === "confirmed") return { rec, chip: `<span class="chip chip--ok">Confirmed</span>` };
  if (rec.outcome === "unconfirmed") return { rec, chip: `<span class="chip chip--warn">Unconfirmed</span>` };
  if (rec.outcome === "mismatch") return { rec, chip: `<span class="chip chip--rej">Did not match</span>` };
  if (rec.outcome === "send_failed") return { rec, chip: `<span class="chip chip--rej">Not sent</span>` };
  return { rec, chip: `<span class="chip chip--wait">${rec.response ? "Waiting for telemetry" : "Waiting for response"}</span>` };
}

function renderRehearsal() {
  if (!ui.config) return setHTML("rehearsal", `<div class="panel__body empty">Loading scenarios…</div>`);
  const run = ui.snap?.run;
  const recs = recordsById();
  const blocker = commandBlocker();
  const busy = !!ui.snap?.in_flight || ui.posting;

  const list = ui.config.scenarios.map((s) => {
    const active = run && run.scenario === s.id;
    return `<div class="scen ${active ? "scen--active" : ""}">
      <div class="scen__text"><div class="scen__title">${esc(s.title)}</div><div class="scen__sum">${esc(s.summary)}</div></div>
      ${active && run.state !== "complete" ? `<span class="chip chip--active">Active</span>`
               : `<button class="btn btn--quiet" data-key="start-${s.id}" data-start="${s.id}" ${busy ? "disabled" : ""}>${active ? "Run again" : "Start"}</button>`}
    </div>`;
  }).join("");

  let body = `<p class="empty" style="margin-top:14px">Choose a scenario. Each step sends one real command to the
    simulator. A step is confirmed only when the command response and the telemetry that follows match the expected result.</p>`;

  if (run) {
    const sc = ui.config.scenarios.find((s) => s.id === run.scenario);
    const t = ui.snap.telemetry;
    const complete = run.state === "complete";
    document.getElementById("reh-meta").textContent = complete ? "Complete" : `Step ${run.step + 1} of ${sc.steps.length}`;

    // Once every setup step is confirmed, the starting conditions were established by evidence;
    // after that the live values are expected to move, so stop comparing against them.
    const setupDone = sc.steps.every((st, i) => !st.setup || run.step_outcomes[i] === "confirmed");
    const cond = (target, sample) => (setupDone ? `<span class="chip chip--ok">Set up</span>` : condState(target, sample));
    const steps = sc.steps.map((st, i) => {
      const { rec, chip } = stepStatus(i, run, recs);
      const current = !complete && i === run.step;
      const done = rec?.outcome === "confirmed";
      let detail = "";
      if (current) {
        const inFlight = rec && rec.phase !== "done";
        const outcome = rec?.outcome;
        let actions;
        if (inFlight) {
          actions = `<button class="btn" disabled>Waiting for evidence…</button>`;
        } else if (outcome === "confirmed") {
          const last = i + 1 >= sc.steps.length;
          actions = `<button class="btn btn--solid" data-key="advance" data-advance="1">${last ? "Finish rehearsal" : "Continue to next step"}</button>`;
        } else if (outcome) {
          actions = `<button class="btn" data-key="send-step" data-send-step="1" ${blocker || busy ? "disabled" : ""}>Send again</button>
            <button class="btn btn--quiet" data-key="stop" data-stop="1">Stop rehearsal</button>`;
        } else {
          actions = `<button class="btn btn--solid" data-key="send-step" data-send-step="1" ${blocker || busy ? "disabled" : ""}>
            Send ${esc(st.action)}</button>`;
        }
        const actualCls = { confirmed: "ok", unconfirmed: "warn", mismatch: "rej", send_failed: "rej" }[outcome] || "";
        detail = `<div class="step__body">
          <dl class="kv">
            <dt>Next action</dt><dd><span class="mono">${esc(st.action)}</span></dd>
            <dt>Expected outcome</dt><dd>${esc(st.outcome)}</dd>
          </dl>
          ${rec ? `<div class="actual actual--${actualCls}"><span class="faint">Actual:</span> ${esc(rec.summary)}</div>` : ""}
          ${outcome && outcome !== "confirmed" ? `<div class="decision">${outcome === "unconfirmed"
              ? "Evidence timed out. Check the timeline, then decide whether to send again."
              : "The result did not match. Check the timeline evidence before sending again."}</div>` : ""}
          ${blocker && !inFlight ? `<div class="decision">${esc(blocker)}</div>` : ""}
          <div class="btn-row">${actions}</div>
        </div>`;
      }
      return `<li class="step ${current ? "step--current" : ""} ${done ? "step--done" : ""}">
        <span class="step__n">${done ? "✓" : i + 1}</span>
        <span class="step__title">${esc(st.title)}<span class="mono">${esc(st.action)}</span>${st.setup ? `<span class="step__tag">setup</span>` : ""}</span>
        ${chip}${detail}</li>`;
    }).join("");

    body = `<div class="run">
      <div class="run__head"><h3>${esc(sc.title)}</h3>
        ${complete ? "" : `<button class="btn btn--quiet" data-key="stop-top" data-stop="1" style="min-height:32px;padding:4px 10px">Stop</button>`}</div>
      <div class="section-k">Starting conditions</div>
      <div class="conds">
        <div class="cond"><div class="cond__k">Flight mode</div><div class="cond__v"><span class="mono">${sc.start.mode}</span>${cond(sc.start.mode, t.mode)}</div></div>
        <div class="cond"><div class="cond__k">Payload power</div><div class="cond__v"><span class="mono">${sc.start.power}</span>${cond(sc.start.power, t.power)}</div></div>
      </div>
      <div class="section-k">Steps</div>
      <ol class="steps">${steps}</ol>
      ${complete ? `<div class="done-banner">Rehearsal complete. Every step was confirmed by the command response and telemetry.</div>
        <div class="btn-row" style="margin-top:10px"><button class="btn btn--quiet" data-key="close" data-stop="1">Close</button></div>` : ""}
    </div>`;
  } else {
    document.getElementById("reh-meta").textContent = "3 scenarios";
  }
  setHTML("rehearsal", `<div class="panel__body"><div class="scen-list ${run ? "scen-list--compact" : ""}">${list}</div>${body}</div>`);
  // Keep the current step in view when the rehearsal moves on.
  const stepKey = run ? `${run.scenario}:${run.step}:${run.state}` : "";
  if (stepKey && stepKey !== ui.lastStepKey) {
    document.querySelector("#rehearsal .step--current, #rehearsal .done-banner")?.scrollIntoView({ block: "nearest" });
  }
  ui.lastStepKey = stepKey;
}

// ------------------------------------------------------------------ timeline
function evRow(key, ev, extraCls = "") {
  return `<div class="ev"><div class="ev__head"><span class="ev__k ${extraCls}">${esc(key)}</span>
    <span class="ev__t">FSW ${esc(fswClock(ev.fsw_time))} · rx ${clock(ev.ground_time)}</span></div>
    <div class="ev__body"><b>${esc(shortName(ev.name))}</b>(${esc((ev.args || []).join(", "))}) · ${esc(ev.severity)}<br>${esc(ev.text)}</div></div>`;
}

function evidence(rec) {
  if (rec.type === "activity") {
    return rec.events.map((e) => evRow(shortName(e.name), e, e.name.endsWith("PowerOnRejected") ? "ev__k--rej" : "")).join("");
  }
  const rows = [];
  rows.push(`<div class="ev"><div class="ev__head"><span class="ev__k">Command sent</span><span class="ev__t">rx ${clock(rec.sent_at)}</span></div>
    <div class="ev__body"><b>${esc(rec.mnemonic)}</b>(${esc(rec.args.join(", "))}) · opcode 0x${Number(rec.opcode).toString(16)}</div></div>`);
  if (rec.dispatched) rows.push(evRow("Dispatched", rec.dispatched));
  if (rec.response) rows.push(evRow(rec.response.status === "OK" ? "Response · accepted" : "Response · rejected", rec.response.event,
    rec.response.status === "OK" ? "" : "ev__k--rej"));
  for (const e of rec.events) {
    const rej = e.name.endsWith("PowerOnRejected");
    rows.push(evRow(rej ? "Rejection event" : "Event", e, rej ? "ev__k--rej" : ""));
  }
  if (rec.telemetry) {
    rows.push(`<div class="ev"><div class="ev__head"><span class="ev__k">Resulting telemetry</span>
      <span class="ev__t">FSW ${esc(fswClock(rec.telemetry.fsw_time))} · rx ${clock(rec.telemetry.ground_time)}</span></div>
      <div class="ev__body"><b>${esc(shortName(rec.telemetry.name))}</b> = ${esc(rec.telemetry.value)}</div></div>`);
  } else if (rec.outcome !== "send_failed") {
    rows.push(`<div class="ev"><div class="ev__head"><span class="ev__k">Resulting telemetry</span></div>
      <div class="ev__body">${rec.phase === "done" ? "None received" : "Waiting"} · ${esc(shortName(rec.channel))}</div></div>`);
  }
  for (const x of rec.late || []) {
    rows.push("value" in x
      ? `<div class="ev"><div class="ev__head"><span class="ev__k ev__k--late">Late telemetry</span>
          <span class="ev__t">FSW ${esc(fswClock(x.fsw_time))} · rx ${clock(x.ground_time)}</span></div>
          <div class="ev__body"><b>${esc(shortName(x.name))}</b> = ${esc(x.value)} · after the evidence window</div></div>`
      : evRow("Late event", x, "ev__k--late"));
  }
  if (rec.checks?.length) {
    const mk = (ok) => (ok === true ? `<span class="mk mk--ok">✓</span>` : ok === false ? `<span class="mk mk--no">✕</span>` : `<span class="mk mk--wait">…</span>`);
    rows.push(`<div class="ev"><div class="ev__head"><span class="ev__k">${rec.origin ? "Expected result" : "Evidence"}</span></div>
      <ul class="checks">${rec.checks.map((c) => `<li>${mk(c.ok)}<span>${esc(c.label)} <span class="faint">· ${esc(c.detail)}</span></span></li>`).join("")}</ul></div>`);
  }
  return rows.join("");
}

function renderTimeline() {
  const items = ui.snap?.timeline || [];
  if (!items.length) {
    return setHTML("timeline", `<li class="empty" style="padding:8px">No commands yet. Sent commands, responses, rejection events and telemetry appear here.</li>`);
  }
  setHTML("timeline", items.map((r) => {
    const open = ui.open.has(r.id);
    const chips = r.type === "activity" ? `<span class="chip chip--warn">Not from console</span>` : responseChip(r) + evidenceChip(r);
    const head = r.type === "activity"
      ? `<span class="tl__line"><span class="tl__cmd">${esc(r.label)}</span></span>
         <span class="tl__chips">${chips}</span>
         <span class="tl__summary">${esc(r.summary)} ${r.events.length} event(s).</span>`
      : `<span class="tl__line"><span class="tl__cmd">${esc(shortName(r.mnemonic))}${r.args.length ? ` ${esc(r.args.join(" "))}` : ""}</span>
           <span class="tl__origin">${esc(originText(r))}</span></span>
         <span class="tl__chips">${chips}</span>
         <span class="tl__summary">${esc(r.summary)}</span>`;
    return `<li class="tl">
      <button class="tl__row" data-key="tl-${r.id}" data-toggle="${r.id}" aria-expanded="${open}">
        <span class="tl__time">${clock(r.sent_at)}</span>
        <span class="tl__main">${head}</span>
        <span class="tl__caret" aria-hidden="true">${open ? "▾" : "▸"}</span>
      </button>
      ${open ? `<div class="tl__ev">${evidence(r)}</div>` : ""}
    </li>`;
  }).join(""));
}

function render() {
  renderLink(); renderStrip(); renderCommands(); renderRehearsal(); renderTimeline();
}

// Ages tick locally between snapshots.
setInterval(() => {
  for (const el of document.querySelectorAll("[data-age]")) {
    const v = Number(el.dataset.age);
    const txt = el.dataset.bare ? age(v).replace(" ago", "") : age(v);
    if (el.textContent !== txt) el.textContent = txt;
  }
}, 250);

// ------------------------------------------------------------------ actions
async function post(path, body) {
  ui.posting = true; ui.notice = null; render();
  try {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) ui.notice = { kind: res.status === 503 ? "off" : "error", text: data.error || `Request failed (${res.status})` };
    else if (data.id != null) {
      if (ui.autoOpen != null) ui.open.delete(ui.autoOpen);  // only the newest command opens by itself
      ui.autoOpen = data.id; ui.open.add(data.id);
    }
  } catch (err) {
    ui.notice = { kind: "off", text: "Console adapter unreachable. Nothing was sent." };
  } finally {
    ui.posting = false; render();
  }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("button, input");
  if (!t || t.disabled) return;
  if (t.dataset.cmd) {
    const body = { kind: t.dataset.cmd };
    if (t.dataset.cmd === "SET_MODE") body.mode = ui.mode;
    post("/api/command", body);
  } else if (t.dataset.start) post("/api/scenario/start", { id: t.dataset.start });
  else if (t.dataset.sendStep) post("/api/scenario/send");
  else if (t.dataset.advance) post("/api/scenario/advance");
  else if (t.dataset.stop) post("/api/scenario/stop");
  else if (t.dataset.toggle) {
    const id = Number(t.dataset.toggle);
    ui.open.has(id) ? ui.open.delete(id) : ui.open.add(id);
    renderTimeline();
  }
});
document.addEventListener("change", (e) => {
  if (e.target.name === "mode") { ui.mode = e.target.value; renderCommands(); }
});

// ------------------------------------------------------------------ data
function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (m) => {
    const snap = JSON.parse(m.data);
    ui.offset = snap.server_time - Date.now() / 1000;
    if (ui.snap && snap.in_flight == null && ui.snap.in_flight != null) ui.notice = null;
    ui.snap = snap; ui.adapter = "online";
    if (!ui.config) loadConfig();
    render();
  };
  es.onerror = () => { ui.adapter = "offline"; render(); };
}

let configLoading = false;
function loadConfig() {
  if (configLoading) return;
  configLoading = true;
  fetch("/api/config").then((r) => r.json()).then((c) => { ui.config = c; render(); })
    .catch(() => {}).finally(() => { configLoading = false; });
}
loadConfig();
render();
connect();
