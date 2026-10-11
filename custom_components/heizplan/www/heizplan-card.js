/*
 * Heizplan-Karte – Übersicht, Wochenplan-Editor und Vorlagen
 * gehört zur Integration custom_components/heizplan (wird von ihr ausgeliefert).
 */
const HP_VERSION = "0.1.0";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mo", tue: "Di", wed: "Mi", thu: "Do", fri: "Fr", sat: "Sa", sun: "So" };
const DAY_LONG = { mon: "Montag", tue: "Dienstag", wed: "Mittwoch", thu: "Donnerstag", fri: "Freitag", sat: "Samstag", sun: "Sonntag" };
const SNAP = 15; // Minuten
const SOURCE_LABEL = { plan: "Plan", away: "Abwesend", override: "Manuell", inactive: "Inaktiv" };

const toMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
const fmtMin = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const fmtT = (t) => (t === null || t === undefined || isNaN(t) ? "–" : Number(t).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 }));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function tempColor(t) {
  // 15 °C blau … 20 °C orange … 25 °C rot
  const x = clamp((Number(t) - 15) / 10, 0, 1);
  const hue = 215 - x * 215;
  const light = 52 - Math.abs(x - 0.5) * 8;
  return `hsl(${hue.toFixed(0)}, 70%, ${light.toFixed(0)}%)`;
}

function dayBlocks(blocks) {
  // -> [{start,end,temp,idx}]
  return blocks.map((b, i) => ({
    start: toMin(b.start),
    end: i + 1 < blocks.length ? toMin(blocks[i + 1].start) : 1440,
    temp: Number(b.temp),
    idx: i,
  }));
}

function todayKey(d = new Date()) { return DAYS[(d.getDay() + 6) % 7]; }

class HeizplanCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._tab = "overview";
    this._room = null;
    this._popup = null; // {day, idx}
    this._copy = null;  // {day}
    this._drag = null;
    this._unsub = null;
    this._error = null;
  }

  setConfig(config) {
    this._config = { view: "overview", ...config };
    this._tab = this._config.view === "week" ? "week" : this._config.view === "templates" ? "templates" : "overview";
    if (config.room) this._room = config.room;
  }

  static getStubConfig() { return { view: "overview" }; }
  getCardSize() { return 8; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._subscribe();
    else if (this._data && !this._drag && !this._popup) this._renderLight();
  }

  connectedCallback() { if (this._hass && !this._unsub) this._subscribe(); }
  disconnectedCallback() {
    if (this._unsub) { this._unsub.then((u) => u && u()).catch(() => {}); this._unsub = null; }
  }

  _subscribe() {
    if (this._unsub || !this._hass) return;
    this._unsub = this._hass.connection
      .subscribeMessage((msg) => { this._data = msg; this._error = null; if (!this._drag && !this._popup && !this._copy) this._render(); }, { type: "heizplan/subscribe" })
      .catch((err) => { this._error = err.message || String(err); this._unsub = null; this._render(); });
  }

  async _call(payload) {
    try {
      const res = await this._hass.callWS(payload);
      this._data = res;
      this._error = null;
      this._render();
      return res;
    } catch (err) {
      this._error = err.message || String(err);
      this._render();
      throw err;
    }
  }

  // --------------------------------------------------------------- Helfer
  _rooms() {
    const rooms = Object.entries(this._data?.rooms || {}).map(([id, r]) => ({ id, ...r }));
    const order = this._config.rooms;
    if (Array.isArray(order) && order.length) {
      return order.map((id) => rooms.find((r) => r.id === id || r.climate === id)).filter(Boolean);
    }
    return rooms.sort((a, b) => a.name.localeCompare(b.name, "de"));
  }

  _currentRoom() {
    const rooms = this._rooms();
    if (!rooms.length) return null;
    return rooms.find((r) => r.id === this._room || r.climate === this._room) || rooms[0];
  }

  _ist(room) {
    const st = this._hass?.states?.[room.climate];
    const v = st?.attributes?.current_temperature;
    return v === undefined ? room.status.current_temp : v;
  }

  _nextText(status) {
    if (!status.next_change) return "";
    const d = new Date(status.next_change);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const tomorrow = new Date(now.getTime() + 86400000).toDateString() === d.toDateString();
    const day = sameDay ? "" : tomorrow ? "morgen " : `${DAY_LABEL[todayKey(d)]} `;
    const time = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    return `ab ${day}${time} → ${fmtT(status.next_temp)} °C`;
  }

  // --------------------------------------------------------------- Rendering
  _renderLight() {
    // nur Ist-Werte aktualisieren (hass-Update), kein kompletter Neuaufbau
    this.shadowRoot.querySelectorAll("[data-ist]").forEach((el) => {
      const room = this._data?.rooms?.[el.dataset.ist];
      if (room) el.textContent = fmtT(this._ist({ ...room, id: el.dataset.ist }));
    });
  }

  _render() {
    if (!this._config) return;
    const title = this._config.title ?? "Heizplan";
    let body;
    if (this._error && !this._data) {
      body = `<div class="err">Heizplan nicht erreichbar: ${esc(this._error)}</div>`;
    } else if (!this._data) {
      body = `<div class="muted pad">Lade …</div>`;
    } else if (!Object.keys(this._data.rooms).length) {
      body = `<div class="muted pad">Noch keine Räume. Räume über den Dienst <code>heizplan.import_scheduler</code> übernehmen oder im Tab „Räume“ anlegen.</div>`;
    } else {
      body = this._tab === "week" ? this._renderWeek() : this._tab === "templates" ? this._renderTemplates() : this._renderOverview();
    }
    const tabs = this._config.hide_tabs ? "" : `
      <div class="tabs">
        ${[["overview", "Übersicht"], ["week", "Wochenplan"], ["templates", "Vorlagen"]]
          .map(([k, l]) => `<button class="tab ${this._tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`).join("")}
      </div>`;
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="title">${esc(title)}</div>
          ${this._data ? this._renderMode() : ""}
        </div>
        ${this._data && !this._data.season_active ? `<div class="banner">Heizperiode ist aus – Heizplan schreibt keine Temperaturen.</div>` : ""}
        ${this._error && this._data ? `<div class="err">${esc(this._error)}</div>` : ""}
        ${tabs}
        <div class="body">${body}</div>
        ${this._popup ? this._renderPopup() : ""}
        ${this._copy ? this._renderCopy() : ""}
      </ha-card>`;
    this._bind();
  }

  _renderMode() {
    const m = this._data.mode;
    return `
      <div class="mode">
        ${[["plan", "Plan"], ["away", "Abwesend"], ["off", "Aus"]]
          .map(([k, l]) => `<button class="seg ${m === k ? "on" : ""}" data-mode="${k}">${l}</button>`).join("")}
        <span class="away" title="Temperatur bei Abwesend">
          <button class="mini" data-away="-0.5">−</button>${fmtT(this._data.away_temp)} °C<button class="mini" data-away="0.5">+</button>
        </span>
      </div>`;
  }

  _renderOverview() {
    const rooms = this._rooms();
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const tk = todayKey(now);
    return `<div class="grid">${rooms.map((r) => {
      const s = r.status;
      const ist = this._ist(r);
      const diff = s.target !== null && ist !== null && ist !== undefined ? ist - s.target : null;
      const state = diff === null ? "" : diff < -0.5 ? "cold" : diff > 0.5 ? "warm" : "ok";
      const blocks = dayBlocks(r.plan[tk]);
      const srcText = s.source === "override" && s.override
        ? `Manuell bis ${new Date(s.override.until).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}`
        : SOURCE_LABEL[s.source] || s.source;
      return `
        <div class="tile">
          <div class="trow">
            <button class="name link" data-open="${r.id}" title="Wochenplan öffnen">${esc(r.name)}</button>
            <span class="badge ${s.source}">${esc(srcText)}</span>
          </div>
          <div class="temps">
            <div><div class="lbl">Ist</div><div class="big ${state}" data-ist="${r.id}">${fmtT(ist)}</div></div>
            <div><div class="lbl">Soll</div><div class="big">${fmtT(s.target)}</div></div>
            ${s.valve !== null && s.valve !== undefined ? `<div><div class="lbl">Ventil</div><div class="mid">${Math.round(s.valve)} %</div></div>` : ""}
          </div>
          <div class="bar small">
            ${blocks.map((b) => `<div class="blk" style="left:${b.start / 14.4}%;width:${(b.end - b.start) / 14.4}%;background:${tempColor(b.temp)}"></div>`).join("")}
            <div class="now" style="left:${nowMin / 14.4}%"></div>
          </div>
          <div class="next">${esc(s.source === "plan" ? this._nextText(s) : s.source === "inactive" ? "" : `Plan: ${fmtT(s.plan_temp)} °C`)}</div>
          <div class="actions">
            <button class="mini" data-ovr="${r.id}" data-delta="-0.5" title="2 h kühler">−</button>
            <button class="mini" data-ovr="${r.id}" data-delta="0.5" title="2 h wärmer">+</button>
            ${s.source === "override" ? `<button class="txt" data-clear="${r.id}">zurück zum Plan</button>` : `<span class="hint">± für 2 Std.</span>`}
          </div>
        </div>`;
    }).join("")}</div>`;
  }

  _renderWeek() {
    const room = this._currentRoom();
    if (!room) return "";
    const rooms = this._rooms();
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const tk = todayKey(now);
    const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24];
    const templates = Object.entries(this._data.templates || {});
    return `
      <div class="chips">${rooms.map((r) => `<button class="chip ${r.id === room.id ? "on" : ""}" data-room="${r.id}">${esc(r.name)}</button>`).join("")}</div>
      <div class="week">
        <div class="scale"><span></span><div class="ticks">${hours.map((h) => `<span style="left:${h / 0.24}%">${h}</span>`).join("")}</div><span></span></div>
        ${DAYS.map((d) => {
          const blocks = dayBlocks(room.plan[d]);
          return `
            <div class="row ${d === tk ? "today" : ""}">
              <span class="day">${DAY_LABEL[d]}</span>
              <div class="bar" data-day="${d}">
                ${blocks.map((b) => `
                  <div class="blk click" data-day="${d}" data-idx="${b.idx}" style="left:${b.start / 14.4}%;width:${(b.end - b.start) / 14.4}%;background:${tempColor(b.temp)}">
                    ${(b.end - b.start) >= 90 ? `<span>${fmtT(b.temp)}°</span>` : ""}
                  </div>
                  ${b.idx > 0 ? `<div class="handle" data-day="${d}" data-idx="${b.idx}" style="left:${b.start / 14.4}%" title="${fmtMin(b.start)} – ziehen zum Verschieben"></div>` : ""}`).join("")}
                ${d === tk ? `<div class="now" style="left:${nowMin / 14.4}%"></div>` : ""}
              </div>
              <button class="mini" data-copy="${d}" title="Tag kopieren">⧉</button>
            </div>`;
        }).join("")}
      </div>
      <div class="legend">
        ${[15, 17, 19, 21, 23, 25].map((t) => `<span><i style="background:${tempColor(t)}"></i>${t}°</span>`).join("")}
        <span class="muted">Block antippen = bearbeiten · Kante ziehen = Zeit verschieben</span>
      </div>
      <div class="tpl">
        <button class="txt" data-save-tpl="${room.id}">Als Vorlage speichern</button>
        ${templates.length ? `
          <select data-apply-sel>
            <option value="">Vorlage übernehmen …</option>
            ${templates.map(([id, t]) => `<option value="${esc(id)}">${esc(t.name)}</option>`).join("")}
          </select>` : ""}
      </div>`;
  }

  _renderTemplates() {
    const templates = Object.entries(this._data.templates || {});
    const rooms = this._rooms();
    if (!templates.length) {
      return `<div class="muted pad">Noch keine Vorlagen. Im Wochenplan eines Raums „Als Vorlage speichern“ wählen.</div>`;
    }
    return templates.map(([id, t]) => `
      <div class="tplcard">
        <div class="trow"><b>${esc(t.name)}</b>
          <span><button class="txt danger" data-del-tpl="${esc(id)}">Löschen</button></span></div>
        <div class="week compact">
          ${DAYS.map((d) => `
            <div class="row"><span class="day">${DAY_LABEL[d]}</span>
              <div class="bar small">${dayBlocks(t.plan[d]).map((b) => `<div class="blk" style="left:${b.start / 14.4}%;width:${(b.end - b.start) / 14.4}%;background:${tempColor(b.temp)}"></div>`).join("")}</div>
            </div>`).join("")}
        </div>
        <div class="apply">
          ${rooms.map((r) => `<label><input type="checkbox" data-tpl-room="${esc(id)}" value="${r.id}"> ${esc(r.name)}</label>`).join("")}
          <button class="txt" data-apply-tpl="${esc(id)}">Auf gewählte Räume anwenden</button>
        </div>
      </div>`).join("");
  }

  _renderPopup() {
    const room = this._currentRoom();
    const { day, idx } = this._popup;
    const blocks = dayBlocks(room.plan[day]);
    const b = blocks[idx];
    if (!b) return "";
    const t = this._popup.temp ?? b.temp;
    return `
      <div class="overlay" data-close>
        <div class="dialog" data-stop>
          <div class="dtitle">${esc(room.name)} · ${DAY_LONG[day]}</div>
          <div class="field"><label>Von</label>
            ${idx === 0 ? `<span>00:00</span>` : `<input type="time" step="900" data-f="start" value="${fmtMin(b.start)}">`}
            <label>bis</label><span>${b.end === 1440 ? "24:00" : fmtMin(b.end)}</span></div>
          <div class="field big-temp">
            <button class="mini" data-t="-0.5">−</button>
            <span class="tv" style="color:${tempColor(t)}">${fmtT(t)} °C</span>
            <button class="mini" data-t="0.5">+</button>
          </div>
          <div class="field"><label>Neuer Block ab</label><input type="time" step="900" data-f="split" placeholder="hh:mm"></div>
          <div class="dact">
            ${idx > 0 ? `<button class="txt danger" data-del-block>Block löschen</button>` : "<span></span>"}
            <span>
              <button class="txt" data-close>Abbrechen</button>
              <button class="txt primary" data-save-block>Speichern</button>
            </span>
          </div>
        </div>
      </div>`;
  }

  _renderCopy() {
    const { day } = this._copy;
    return `
      <div class="overlay" data-close>
        <div class="dialog" data-stop>
          <div class="dtitle">${DAY_LONG[day]} kopieren nach</div>
          <div class="days">${DAYS.filter((d) => d !== day).map((d) => `<label><input type="checkbox" data-copy-day value="${d}"> ${DAY_LABEL[d]}</label>`).join("")}</div>
          <div class="quick">
            <button class="txt" data-pick="mon,tue,wed,thu,fri">Werktage</button>
            <button class="txt" data-pick="sat,sun">Wochenende</button>
            <button class="txt" data-pick="${DAYS.join(",")}">Alle</button>
          </div>
          <div class="dact"><span></span><span>
            <button class="txt" data-close>Abbrechen</button>
            <button class="txt primary" data-do-copy>Kopieren</button></span></div>
        </div>
      </div>`;
  }

  // --------------------------------------------------------------- Aktionen
  _bind() {
    const $ = (sel) => this.shadowRoot.querySelectorAll(sel);
    // Temperatur-Beschriftung ausblenden, wenn der Block zu schmal ist
    requestAnimationFrame(() => $(".blk span").forEach((sp) => {
      if (sp.parentElement.clientWidth < sp.scrollWidth + 6) sp.style.visibility = "hidden";
    }));
    $("[data-tab]").forEach((el) => el.addEventListener("click", () => { this._tab = el.dataset.tab; this._render(); }));
    $("[data-mode]").forEach((el) => el.addEventListener("click", () => this._call({ type: "heizplan/set_mode", mode: el.dataset.mode })));
    $("[data-away]").forEach((el) => el.addEventListener("click", () =>
      this._call({ type: "heizplan/set_away_temp", temp: this._data.away_temp + Number(el.dataset.away) })));
    $("[data-open]").forEach((el) => el.addEventListener("click", () => { this._room = el.dataset.open; this._tab = "week"; this._render(); }));
    $("[data-room]").forEach((el) => el.addEventListener("click", () => { this._room = el.dataset.room; this._render(); }));
    $("[data-ovr]").forEach((el) => el.addEventListener("click", () => {
      const room = this._data.rooms[el.dataset.ovr];
      const base = room.status.target ?? room.status.plan_temp;
      this._call({ type: "heizplan/set_override", room_id: el.dataset.ovr, temp: base + Number(el.dataset.delta), minutes: 120 });
    }));
    $("[data-clear]").forEach((el) => el.addEventListener("click", () => this._call({ type: "heizplan/clear_override", room_id: el.dataset.clear })));

    // Wochenplan
    $(".blk.click").forEach((el) => el.addEventListener("click", (ev) => {
      if (this._justDragged) return;
      ev.stopPropagation();
      this._popup = { day: el.dataset.day, idx: Number(el.dataset.idx) };
      this._render();
    }));
    $(".handle").forEach((el) => el.addEventListener("pointerdown", (ev) => this._startDrag(ev, el)));
    $("[data-copy]").forEach((el) => el.addEventListener("click", () => { this._copy = { day: el.dataset.copy }; this._render(); }));
    $("[data-save-tpl]").forEach((el) => el.addEventListener("click", () => {
      const room = this._data.rooms[el.dataset.saveTpl];
      const name = prompt("Name der Vorlage:", `${room.name}`);
      if (name) this._call({ type: "heizplan/save_template", name, plan: room.plan });
    }));
    $("[data-apply-sel]").forEach((el) => el.addEventListener("change", () => {
      const room = this._currentRoom();
      if (el.value && confirm(`Vorlage für ${room.name} übernehmen? Der bisherige Plan wird ersetzt.`)) {
        this._call({ type: "heizplan/apply_template", template_id: el.value, room_ids: [room.id] });
      } else el.value = "";
    }));

    // Vorlagen
    $("[data-del-tpl]").forEach((el) => el.addEventListener("click", () => {
      if (confirm("Vorlage löschen?")) this._call({ type: "heizplan/delete_template", template_id: el.dataset.delTpl });
    }));
    $("[data-apply-tpl]").forEach((el) => el.addEventListener("click", () => {
      const id = el.dataset.applyTpl;
      const ids = [...this.shadowRoot.querySelectorAll(`[data-tpl-room="${CSS.escape(id)}"]:checked`)].map((c) => c.value);
      if (!ids.length) return;
      if (confirm(`Vorlage auf ${ids.length} Raum/Räume anwenden? Die bisherigen Pläne werden ersetzt.`)) {
        this._call({ type: "heizplan/apply_template", template_id: id, room_ids: ids });
      }
    }));

    // Dialoge
    $("[data-close]").forEach((el) => el.addEventListener("click", (ev) => {
      if (ev.target !== el) return;
      this._popup = null; this._copy = null; this._render();
    }));
    $("[data-t]").forEach((el) => el.addEventListener("click", () => {
      const room = this._currentRoom();
      const b = dayBlocks(room.plan[this._popup.day])[this._popup.idx];
      const startVal = this.shadowRoot.querySelector('[data-f="start"]')?.value;
      const splitVal = this.shadowRoot.querySelector('[data-f="split"]')?.value;
      this._popup.temp = clamp((this._popup.temp ?? b.temp) + Number(el.dataset.t), 5, 30);
      this._popup.startVal = startVal; this._popup.splitVal = splitVal;
      this._render();
      if (startVal) this.shadowRoot.querySelector('[data-f="start"]').value = startVal;
      if (splitVal) this.shadowRoot.querySelector('[data-f="split"]').value = splitVal;
    }));
    $("[data-save-block]").forEach((el) => el.addEventListener("click", () => this._saveBlock()));
    $("[data-del-block]").forEach((el) => el.addEventListener("click", () => this._deleteBlock()));
    $("[data-pick]").forEach((el) => el.addEventListener("click", () => {
      const pick = el.dataset.pick.split(",");
      this.shadowRoot.querySelectorAll("[data-copy-day]").forEach((c) => { c.checked = pick.includes(c.value); });
    }));
    $("[data-do-copy]").forEach((el) => el.addEventListener("click", () => {
      const room = this._currentRoom();
      const day = this._copy.day;
      const to = [...this.shadowRoot.querySelectorAll("[data-copy-day]:checked")].map((c) => c.value);
      this._copy = null;
      if (!to.length) { this._render(); return; }
      this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks: room.plan[day], copy_to: to });
    }));
  }

  _saveBlock() {
    const room = this._currentRoom();
    const { day, idx } = this._popup;
    const blocks = room.plan[day].map((b) => ({ ...b }));
    const block = blocks[idx];
    const temp = this._popup.temp ?? Number(block.temp);
    block.temp = temp;
    const startEl = this.shadowRoot.querySelector('[data-f="start"]');
    if (startEl && startEl.value && idx > 0) {
      const prev = toMin(blocks[idx - 1].start);
      const next = idx + 1 < blocks.length ? toMin(blocks[idx + 1].start) : 1440;
      const m = clamp(Math.round(toMin(startEl.value) / SNAP) * SNAP, prev + SNAP, next - SNAP);
      block.start = fmtMin(m);
    }
    const splitEl = this.shadowRoot.querySelector('[data-f="split"]');
    if (splitEl && splitEl.value) {
      const m = Math.round(toMin(splitEl.value) / SNAP) * SNAP;
      if (m > 0 && m < 1440 && !blocks.some((b) => toMin(b.start) === m)) {
        // neuer Block übernimmt zunächst die Temperatur des Blocks, in dem er liegt (+1 °C als Startwert)
        const host = dayBlocks(blocks).find((b) => b.start < m && m < b.end) || dayBlocks(blocks)[0];
        blocks.push({ start: fmtMin(m), temp: clamp(host.temp + 1, 5, 30) });
        blocks.sort((a, b) => toMin(a.start) - toMin(b.start));
      }
    }
    this._popup = null;
    this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks });
  }

  _deleteBlock() {
    const room = this._currentRoom();
    const { day, idx } = this._popup;
    const blocks = room.plan[day].filter((_, i) => i !== idx);
    this._popup = null;
    this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks });
  }

  _startDrag(ev, handle) {
    ev.preventDefault();
    const bar = handle.parentElement;
    const room = this._currentRoom();
    const day = handle.dataset.day;
    const idx = Number(handle.dataset.idx);
    const blocks = room.plan[day].map((b) => ({ ...b }));
    const prev = toMin(blocks[idx - 1].start);
    const next = idx + 1 < blocks.length ? toMin(blocks[idx + 1].start) : 1440;
    const rect = bar.getBoundingClientRect();
    const tip = document.createElement("div");
    tip.className = "tip";
    bar.appendChild(tip);
    this._drag = { day, idx, blocks };
    handle.setPointerCapture(ev.pointerId);
    const cur = handle.previousElementSibling; // Block idx
    const before = [...bar.querySelectorAll(".blk")][idx - 1];
    const move = (e) => {
      const x = clamp((e.clientX - rect.left) / rect.width, 0, 1);
      const m = clamp(Math.round((x * 1440) / SNAP) * SNAP, prev + SNAP, next - SNAP);
      blocks[idx].start = fmtMin(m);
      handle.style.left = `${m / 14.4}%`;
      if (cur) { cur.style.left = `${m / 14.4}%`; cur.style.width = `${(next - m) / 14.4}%`; }
      if (before) before.style.width = `${(m - prev) / 14.4}%`;
      tip.style.left = `${m / 14.4}%`;
      tip.textContent = fmtMin(m);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      this._drag = null;
      this._justDragged = true;
      setTimeout(() => { this._justDragged = false; }, 300);
      this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
    move(ev);
  }
}

const STYLE = `
  :host { --hp-gap: 12px; }
  ha-card { display:block; padding: 12px 16px 16px; position: relative; overflow: hidden; min-height: 120px; }
  .head { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px; }
  .title { font-size: 1.3em; font-weight: 500; }
  .mode { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .seg, .tab, .chip { border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color);
    padding:6px 12px; border-radius:16px; cursor:pointer; font:inherit; font-size:.9em; }
  .seg.on, .tab.on, .chip.on { background:var(--primary-color); color:var(--text-primary-color, #fff); border-color:var(--primary-color); }
  .away { display:inline-flex; align-items:center; gap:4px; font-size:.9em; color:var(--secondary-text-color); margin-left:4px; }
  .mini { width:30px; height:30px; border-radius:50%; border:1px solid var(--divider-color); background:var(--card-background-color);
    color:var(--primary-text-color); cursor:pointer; font-size:1.1em; line-height:1; padding:0; }
  .txt { border:none; background:none; color:var(--primary-color); cursor:pointer; font:inherit; padding:6px 8px; border-radius:6px; }
  .txt:hover { background: rgba(127,127,127,.12); }
  .txt.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); }
  .txt.danger { color: var(--error-color, #db4437); }
  .tabs { display:flex; gap:6px; margin:12px 0 8px; }
  .banner { margin-top:10px; padding:8px 12px; border-radius:8px; background: rgba(255,152,0,.15); color: var(--primary-text-color); }
  .err { margin-top:10px; padding:8px 12px; border-radius:8px; background: rgba(219,68,55,.15); }
  .muted { color: var(--secondary-text-color); }
  .pad { padding: 16px 0; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: var(--hp-gap); }
  .tile { border:1px solid var(--divider-color); border-radius:12px; padding:10px 12px; display:flex; flex-direction:column; gap:8px; }
  .trow { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .name { font-weight:600; font-size:1.05em; }
  .link { border:none; background:none; padding:0; color:var(--primary-text-color); cursor:pointer; font:inherit; font-weight:600; text-align:left; }
  .badge { font-size:.75em; padding:2px 8px; border-radius:10px; background: rgba(127,127,127,.15); white-space:nowrap; }
  .badge.override { background: rgba(255,152,0,.25); }
  .badge.away { background: rgba(33,150,243,.2); }
  .badge.inactive { opacity:.7; }
  .temps { display:flex; gap:18px; align-items:flex-end; }
  .lbl { font-size:.75em; color:var(--secondary-text-color); }
  .big { font-size:1.6em; font-weight:500; }
  .big.cold { color:#2b7bd8; } .big.warm { color:#e8590c; } .big.ok { color: var(--primary-text-color); }
  .mid { font-size:1.15em; }
  .next { font-size:.85em; color:var(--secondary-text-color); min-height:1.2em; }
  .actions { display:flex; align-items:center; gap:6px; }
  .hint { font-size:.8em; color:var(--secondary-text-color); }
  .bar { position:relative; height:34px; border-radius:6px; background: rgba(127,127,127,.12); flex:1; touch-action:none; }
  .bar.small { height:10px; border-radius:5px; overflow:hidden; }
  .tile .bar, .tplcard .bar { flex: none; width: 100%; }
  .week.compact .row .bar { flex: 1; }
  .blk { position:absolute; top:0; bottom:0; display:flex; align-items:center; justify-content:center;
    color:#fff; font-size:.8em; font-weight:600; text-shadow:0 0 2px rgba(0,0,0,.5); overflow:hidden; box-sizing:border-box; }
  .row .bar .blk { border-right:1px solid rgba(255,255,255,.5); }
  .blk.click { cursor:pointer; }
  .blk.click:hover { filter: brightness(1.1); }
  .handle { position:absolute; top:-3px; bottom:-3px; width:14px; margin-left:-7px; cursor:ew-resize; z-index:2; }
  .handle::after { content:""; position:absolute; left:6px; top:6px; bottom:6px; width:2px; background:#fff; border-radius:1px; box-shadow:0 0 2px rgba(0,0,0,.6); }
  .tip { position:absolute; top:-26px; transform:translateX(-50%); background:var(--primary-text-color); color:var(--card-background-color);
    font-size:.75em; padding:2px 6px; border-radius:4px; pointer-events:none; z-index:3; }
  .now { position:absolute; top:-2px; bottom:-2px; width:2px; background: var(--primary-text-color); opacity:.8; z-index:1; pointer-events:none; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin: 4px 0 12px; }
  .week { display:flex; flex-direction:column; gap:6px; }
  .week.compact { gap:3px; }
  .row { display:flex; align-items:center; gap:8px; }
  .row.today .day { color: var(--primary-color); font-weight:700; }
  .day { width:24px; font-size:.9em; }
  .scale { display:flex; gap:8px; font-size:.75em; color:var(--secondary-text-color); }
  .scale > span:first-child { width:24px; } .scale > span:last-child { width:30px; }
  .ticks { position:relative; flex:1; height:14px; }
  .ticks span { position:absolute; transform:translateX(-50%); }
  .legend { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-top:10px; font-size:.8em; }
  .legend i { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:4px; vertical-align:-2px; }
  .tpl { display:flex; gap:8px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  select { font:inherit; padding:6px; border-radius:6px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); }
  .tplcard { border:1px solid var(--divider-color); border-radius:12px; padding:10px 12px; margin-bottom:12px; }
  .apply { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; margin-top:10px; font-size:.9em; }
  .overlay { position:absolute; inset:0; background: rgba(0,0,0,.35); display:flex; align-items:center; justify-content:center; z-index:10; }
  .dialog { background: var(--card-background-color); border-radius:12px; padding:16px; width: min(340px, 90%); box-shadow: 0 6px 24px rgba(0,0,0,.3); }
  .dtitle { font-weight:600; margin-bottom:12px; }
  .field { display:flex; align-items:center; gap:10px; margin:10px 0; }
  .field label { color: var(--secondary-text-color); font-size:.9em; }
  .field input { font:inherit; padding:4px 6px; border-radius:6px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); }
  .big-temp { justify-content:center; gap:16px; }
  .tv { font-size:1.8em; font-weight:600; min-width:110px; text-align:center; }
  .dact { display:flex; justify-content:space-between; align-items:center; margin-top:12px; }
  .days { display:flex; flex-wrap:wrap; gap:10px; }
  .quick { display:flex; gap:4px; margin-top:8px; }
  @media (max-width: 500px) { .ticks span:nth-child(even) { display:none; } .temps { gap:12px; } }
`;

if (!customElements.get("heizplan-card")) {
  customElements.define("heizplan-card", HeizplanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "heizplan-card", name: "Heizplan", description: "Wochenpläne, Soll/Ist je Raum, Vorlagen", preview: false });
  console.info(`%c HEIZPLAN %c ${HP_VERSION} `, "background:#e8590c;color:#fff", "");
}
