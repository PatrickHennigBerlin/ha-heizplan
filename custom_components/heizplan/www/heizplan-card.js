/*
 * Heizplan-Karte – Übersicht, Wochenplan-Editor (je Raum oder alle Räume), Vorlagen
 * gehört zur Integration custom_components/heizplan (wird von ihr ausgeliefert).
 */
const HP_VERSION = "0.5.2";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mo", tue: "Di", wed: "Mi", thu: "Do", fri: "Fr", sat: "Sa", sun: "So" };
const DAY_LONG = { mon: "Montag", tue: "Dienstag", wed: "Mittwoch", thu: "Donnerstag", fri: "Freitag", sat: "Samstag", sun: "Sonntag" };
const SNAP = 15; // Minuten
const ALL = "__all__";
const PRESET_LABEL = { building_protection: "Frostschutz", standby: "Standby", economy: "Nacht" };
const SOURCE_LABEL = { plan: "Plan", away: "Abwesend", override: "Manuell", inactive: "Inaktiv" };

const toMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
const fmtMin = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const fmtT = (t) => (t === null || t === undefined || isNaN(t) ? "–" : Number(t).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 }));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isSwitch = (room) => room && room.kind === "switch";

function tempColor(t) {
  // 15 °C blau … 20 °C grün/gelb … 25 °C rot
  const x = clamp((Number(t) - 15) / 10, 0, 1);
  const hue = 215 - x * 215;
  const light = 52 - Math.abs(x - 0.5) * 8;
  return `hsl(${hue.toFixed(0)}, 70%, ${light.toFixed(0)}%)`;
}
const valueColor = (v) => (typeof v === "boolean" ? (v ? "#e8590c" : "#9e9e9e") : tempColor(v));
const valueText = (v, short = false) => (typeof v === "boolean" ? (v ? "An" : "Aus") : `${fmtT(v)}${short ? "°" : " °C"}`);

function blockValue(b) { return "on" in b ? Boolean(b.on) : Number(b.temp); }

function dayBlocks(blocks) {
  // -> [{start,end,value,idx}]
  return blocks.map((b, i) => ({
    start: toMin(b.start),
    end: i + 1 < blocks.length ? toMin(blocks[i + 1].start) : 1440,
    value: blockValue(b),
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
    this._day = todayKey();
    this._popup = null; // {room, day, idx, value}
    this._copy = null;  // {room, day}
    this._drag = null;
    this._unsub = null;
    this._error = null;
  }

  setConfig(config) {
    this._config = { view: "overview", ...config };
    this._tab = ["week", "templates", "season"].includes(this._config.view) ? this._config.view : "overview";
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
      .subscribeMessage((msg) => { this._data = msg; this._error = null; if (!this._drag && !this._popup && !this._copy && !this._seasonDraft) this._render(); }, { type: "heizplan/subscribe" })
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
    // Heizkörper zuerst (alphabetisch), Schalt-Räume (Fußbodenheizung) danach
    return rooms.sort((a, b) => (isSwitch(a) - isSwitch(b)) || a.name.localeCompare(b.name, "de"));
  }

  _roomById(id) {
    const r = this._data?.rooms?.[id];
    return r ? { id, ...r } : null;
  }

  _currentRoom() {
    const rooms = this._rooms();
    if (!rooms.length) return null;
    return rooms.find((r) => r.id === this._room || r.climate === this._room) || rooms[0];
  }

  _ist(room) {
    if (isSwitch(room)) {
      const st = this._hass?.states?.[room.climate];
      return st ? st.state === "on" : room.status.is_on;
    }
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
    return `ab ${day}${time} → ${valueText(status.next_temp)}`;
  }

  // --------------------------------------------------------------- Rendering
  _renderLight() {
    this.shadowRoot.querySelectorAll("[data-ist]").forEach((el) => {
      const room = this._roomById(el.dataset.ist);
      if (!room) return;
      const ist = this._ist(room);
      el.textContent = isSwitch(room) ? (ist ? "an" : "aus") : fmtT(ist);
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
      body = `<div class="muted pad">Noch keine Räume. Räume über den Dienst <code>heizplan.import_scheduler</code> übernehmen.</div>`;
    } else {
      body = this._tab === "week" ? this._renderWeek() : this._tab === "templates" ? this._renderTemplates() : this._tab === "season" ? this._renderSeason() : this._renderOverview();
    }
    const tabs = this._config.hide_tabs ? "" : `
      <div class="tabs">
        ${[["overview", "Übersicht"], ["week", "Wochenplan"], ["templates", "Vorlagen"], ["season", "Heizperiode"]]
          .map(([k, l]) => `<button class="tab ${this._tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`).join("")}
      </div>`;
    const html = `
        <div class="head">
          <div class="title">${esc(title)}</div>
          ${this._data ? this._renderMode() : ""}
        </div>
        ${this._data && !this._data.season_active ? `<div class="banner" data-tab="season">Heizperiode ist aus – Heizplan schreibt keine Temperaturen. <span class="blink">Heizperiode ›</span></div>` : ""}
        ${this._error && this._data ? `<div class="err">${esc(this._error)}</div>` : ""}
        ${tabs}
        <div class="body">${body}</div>
        ${this._popup ? this._renderPopup() : ""}
        ${this._copy ? this._renderCopy() : ""}`;
    // Gleicher Inhalt -> nichts tun (z. B. Antwort auf eigenen Aufruf + Push vom Server)
    if (html === this._lastHtml && this._card?.isConnected) return;
    this._lastHtml = html;

    // Karte und Stil nur einmal anlegen: das ha-card-Element bleibt bestehen,
    // nur sein Inhalt wird ersetzt. So springt die Seite nicht nach oben.
    if (!this._card || this._card.getRootNode() !== this.shadowRoot) {
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card></ha-card>`;
      this._card = this.shadowRoot.querySelector("ha-card");
    }
    const card = this._card;
    const keep = this._snapshot(card);
    card.style.minHeight = keep.height ? `${keep.height}px` : "";
    card.innerHTML = html;
    this._bind();
    this._restore(card, keep);
  }

  // Scroll-Positionen, Höhe und Fokus vor dem Neuzeichnen merken ...
  _snapshot(card) {
    const scrollers = [];
    let n = this;
    while (n) {
      if (n.scrollTop || n.scrollLeft) scrollers.push([n, n.scrollTop, n.scrollLeft]);
      n = n.parentNode || n.host;
    }
    const inner = [...card.querySelectorAll(".tabs, .chips, .weekwrap, .scroll")].map((el) => el.scrollLeft);
    const act = this.shadowRoot.activeElement;
    let focus = null;
    if (act && act !== card) {
      focus = act.tagName + [...act.attributes].filter((a) => a.name.startsWith("data-")).map((a) => `[${a.name}="${CSS.escape(a.value)}"]`).join("");
    }
    return { x: window.scrollX, y: window.scrollY, scrollers, inner, focus, height: card.offsetHeight };
  }

  // ... und danach wiederherstellen
  _restore(card, k) {
    const apply = () => {
      for (const [el, top, left] of k.scrollers) { if (el.scrollTop !== top) el.scrollTop = top; if (el.scrollLeft !== left) el.scrollLeft = left; }
      if (window.scrollY !== k.y || window.scrollX !== k.x) window.scrollTo(k.x, k.y);
      [...card.querySelectorAll(".tabs, .chips, .weekwrap, .scroll")].forEach((el, i) => { if (k.inner[i]) el.scrollLeft = k.inner[i]; });
    };
    if (k.focus) {
      try { card.querySelector(k.focus)?.focus({ preventScroll: true }); } catch (e) { /* ungültiger Selektor */ }
    }
    apply();
    requestAnimationFrame(() => { apply(); card.style.minHeight = ""; });
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

  _auxIcon(s) {
    const a = s.aux_heating;
    if (!a) return "";
    if (a.boost) return `<span class="aux on" title="Boost: Klima heizt mit, bis der Soll erreicht ist"><ha-icon icon="mdi:rocket-launch-outline"></ha-icon>Boost</span>`;
    if (a.active) return `<span class="aux on" title="Klima heizt mit"><ha-icon icon="mdi:air-conditioner"></ha-icon>Klima</span>`;
    if (a.paused) return `<span class="aux" title="Klima von Hand geändert – Zuheizen pausiert">Klima pausiert</span>`;
    if (a.demand && a.enabled) return `<span class="aux wait" title="Zu kalt – Klima startet nach der Wartezeit">Klima bald</span>`;
    return "";
  }

  _miniBar(blocks, nowMin) {
    return `<div class="bar small">
      ${blocks.map((b) => `<div class="blk" style="left:${b.start / 14.4}%;width:${(b.end - b.start) / 14.4}%;background:${valueColor(b.value)}"></div>`).join("")}
      ${nowMin !== null ? `<div class="now" style="left:${nowMin / 14.4}%"></div>` : ""}
    </div>`;
  }

  _renderOverview() {
    const rooms = this._rooms();
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const tk = todayKey(now);
    return `<div class="grid">${rooms.map((r) => {
      const s = r.status;
      const sw = isSwitch(r);
      const ist = this._ist(r);
      let state = "";
      if (!sw && s.target !== null && ist !== null && ist !== undefined) {
        const diff = ist - s.target;
        state = diff < -0.5 ? "cold" : diff > 0.5 ? "warm" : "ok";
      }
      const srcText = s.source === "override" && s.override
        ? `Manuell bis ${new Date(s.override.until).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}`
        : SOURCE_LABEL[s.source] || s.source;
      const istHtml = sw ? (ist ? "an" : "aus") : fmtT(ist);
      const sollHtml = s.target === null ? "–" : sw ? (s.target ? "An" : "Aus") : fmtT(s.target);
      const actions = sw
        ? `<button class="txt" data-ovr-on="${r.id}" data-val="1">2 Std. an</button>
           <button class="txt" data-ovr-on="${r.id}" data-val="0">2 Std. aus</button>`
        : `<button class="mini" data-ovr="${r.id}" data-delta="-0.5" title="2 h kühler">−</button>
           <button class="mini" data-ovr="${r.id}" data-delta="0.5" title="2 h wärmer">+</button>`;
      return `
        <div class="tile ${sw ? "switch" : ""}">
          <div class="trow">
            <button class="name link" data-open="${r.id}" title="Wochenplan öffnen">${esc(r.name)}</button>
            <span class="tright">${this._auxIcon(s)}<span class="badge ${s.source}">${esc(srcText)}</span></span>
          </div>
          <div class="temps">
            <div><div class="lbl">Ist</div><div class="big ${state}" data-ist="${r.id}">${istHtml}</div></div>
            <div><div class="lbl">Soll</div><div class="big">${sollHtml}</div></div>
            ${s.valve !== null && s.valve !== undefined ? `<div><div class="lbl">Ventil</div><div class="mid">${Math.round(s.valve)} %</div></div>` : ""}
          </div>
          ${this._miniBar(dayBlocks(r.plan[tk]), nowMin)}
          ${!sw && s.source !== "inactive" && s.climate_preset && s.climate_preset !== "comfort" ? `<div class="warn">Regler steht auf ${esc(PRESET_LABEL[s.climate_preset] || s.climate_preset)} – wird automatisch auf Komfort zurückgestellt</div>` : ""}
          <div class="next">${esc(s.source === "plan" ? this._nextText(s) : s.source === "inactive" ? (sw ? "außerhalb der Saison" : "") : `Plan: ${valueText(s.plan_temp)}`)}</div>
          <div class="actions">
            ${actions}
            ${s.source === "override" ? `<button class="txt" data-clear="${r.id}">zurück zum Plan</button>` : sw ? "" : `<span class="hint">± für 2 Std.</span>`}
          </div>
          ${s.aux_heating && !sw ? (s.aux_heating.boost
            ? `<button class="boost on" data-boost="${r.id}" data-on="0" title="Boost abbrechen"><ha-icon icon="mdi:rocket-launch"></ha-icon>Boost läuft – bis ${fmtT(s.target)} °C · abbrechen</button>`
            : `<button class="boost" data-boost="${r.id}" data-on="1" ${s.source === "inactive" || (s.current_temp != null && s.target != null && s.current_temp >= s.target) ? "disabled" : ""} title="${s.current_temp != null && s.target != null && s.current_temp >= s.target ? "Soll ist schon erreicht" : "Klima heizt mit, bis der Soll erreicht ist, dann geht sie wieder aus"}"><ha-icon icon="mdi:rocket-launch-outline"></ha-icon>Boost mit Klima</button>`) : ""}
        </div>`;
    }).join("")}</div>`;
  }

  _rowBar(room, day, nowMin) {
    const blocks = dayBlocks(room.plan[day]);
    return `
      <div class="bar" data-room="${room.id}" data-day="${day}">
        ${blocks.map((b) => `
          <div class="blk click" data-room="${room.id}" data-day="${day}" data-idx="${b.idx}" style="left:${b.start / 14.4}%;width:${(b.end - b.start) / 14.4}%;background:${valueColor(b.value)}">
            <span data-short="${typeof b.value === "boolean" ? (b.value ? "An" : "Aus") : (Number.isInteger(Number(b.value)) ? `${Number(b.value)}°` : fmtT(b.value))}">${valueText(b.value, true)}</span>
          </div>
          ${b.idx > 0 ? `<div class="handle" data-room="${room.id}" data-day="${day}" data-idx="${b.idx}" style="left:${b.start / 14.4}%" title="${fmtMin(b.start)} – ziehen zum Verschieben"></div>` : ""}`).join("")}
        ${nowMin !== null ? `<div class="now" style="left:${nowMin / 14.4}%"></div>` : ""}
      </div>`;
  }

  _scale(labelWidth) {
    const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24];
    return `<div class="scale"><span style="width:${labelWidth}"></span><div class="ticks">${hours.map((h) => `<span style="left:${h / 0.24}%">${h}</span>`).join("")}</div><span class="sp"></span></div>`;
  }

  _renderWeek() {
    const rooms = this._rooms();
    const all = this._room === ALL;
    const room = all ? null : this._currentRoom();
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const tk = todayKey(now);
    const chips = `<div class="chips">
      <button class="chip ${all ? "on" : ""}" data-room-chip="${ALL}">Alle Räume</button>
      ${rooms.map((r) => `<button class="chip ${!all && r.id === room.id ? "on" : ""}" data-room-chip="${r.id}">${esc(r.name)}</button>`).join("")}
    </div>`;
    const legend = `<div class="legend">
        ${[15, 17, 19, 21, 23, 25].map((t) => `<span><i style="background:${tempColor(t)}"></i>${t}°</span>`).join("")}
        ${rooms.some(isSwitch) ? `<span><i style="background:${valueColor(true)}"></i>An</span><span><i style="background:${valueColor(false)}"></i>Aus</span>` : ""}
        <span class="muted">Block antippen = bearbeiten · Kante ziehen = Zeit verschieben</span>
      </div>`;

    if (all) {
      const day = this._day;
      return `${chips}
        <div class="chips days">${DAYS.map((d) => `<button class="chip ${d === day ? "on" : ""}" data-daychip="${d}">${DAY_LABEL[d]}${d === tk ? " ·" : ""}</button>`).join("")}</div>
        <div class="week all">
          ${this._scale("110px")}
          ${rooms.map((r) => `
            <div class="row">
              <button class="rname link" data-room-chip="${r.id}" title="Wochenplan ${esc(r.name)}">${esc(r.name)}</button>
              ${this._rowBar(r, day, day === tk ? nowMin : null)}
              <button class="mini" data-copy="${day}" data-copy-room="${r.id}" title="Tag kopieren">⧉</button>
            </div>`).join("")}
        </div>
        ${legend}`;
    }

    const templates = Object.entries(this._data.templates || {}).filter(([, t]) => (t.kind || "climate") === (room.kind || "climate"));
    return `${chips}
      <div class="week">
        ${this._scale("24px")}
        ${DAYS.map((d) => `
            <div class="row ${d === tk ? "today" : ""}">
              <span class="day">${DAY_LABEL[d]}</span>
              ${this._rowBar(room, d, d === tk ? nowMin : null)}
              <button class="mini" data-copy="${d}" data-copy-room="${room.id}" title="Tag kopieren">⧉</button>
            </div>`).join("")}
      </div>
      ${legend}
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
    return templates.map(([id, t]) => {
      const kind = t.kind || "climate";
      const fit = rooms.filter((r) => (r.kind || "climate") === kind);
      return `
      <div class="tplcard">
        <div class="trow"><b>${esc(t.name)}</b>
          <span><button class="txt danger" data-del-tpl="${esc(id)}">Löschen</button></span></div>
        <div class="week compact">
          ${DAYS.map((d) => `
            <div class="row"><span class="day">${DAY_LABEL[d]}</span>${this._miniBar(dayBlocks(t.plan[d]), null)}</div>`).join("")}
        </div>
        <div class="apply">
          ${fit.map((r) => `<label><input type="checkbox" data-tpl-room="${esc(id)}" value="${r.id}"> ${esc(r.name)}</label>`).join("")}
          <button class="txt" data-apply-tpl="${esc(id)}">Auf gewählte Räume anwenden</button>
        </div>
      </div>`;
    }).join("");
  }

  _renderSeason() {
    const a = this._seasonDraft || this._data.season_auto || {};
    const st = this._data.season_status || {};
    const ent = this._data.season_entity;
    const on = this._data.season_active;
    const weathers = Object.keys(this._hass?.states || {}).filter((e) => e.startsWith("weather.")).sort();
    const dm = (v) => /^\d{2}-\d{2}$/.test(String(v || "")) ? `${String(v).slice(3)}.${String(v).slice(0, 2)}.` : (v ?? "");
    const num = (v) => (v === undefined || v === null ? "" : String(v).replace(".", ","));
    const field = (key, label, value, extra = "") =>
      `<label class="sfield"><span>${label}</span><input data-sa-field="${key}" value="${esc(value)}" ${extra}></label>`;
    const basisLabel = { mean: "Tagesmittel", max: "Höchstwert", min: "Tiefstwert" };
    let detail = "";
    if (a.mode === "dates") {
      detail = `<div class="sgrid">
          ${field("on_date", "An ab (TT.MM)", dm(a.on_date), 'inputmode="decimal" placeholder="01.10."')}
          ${field("off_date", "Aus ab (TT.MM)", dm(a.off_date), 'inputmode="decimal" placeholder="01.05."')}
        </div>
        <div class="hint">Schaltet am jeweiligen Tag um 0:05 (bzw. beim Start von Home Assistant an diesem Tag). Dazwischen bleibt Schalten von Hand möglich.</div>`;
    } else if (a.mode === "weather") {
      detail = `<div class="sgrid">
          <label class="sfield wide"><span>Wetter</span><select data-sa-field="weather_entity">
            ${weathers.length ? "" : `<option value="">keine Wetter-Entität gefunden</option>`}
            ${weathers.map((w) => `<option value="${esc(w)}" ${w === a.weather_entity ? "selected" : ""}>${esc(this._hass.states[w].attributes.friendly_name || w)}</option>`).join("")}
          </select></label>
          ${field("on_below", "An, wenn alle unter (°C)", num(a.on_below), 'inputmode="decimal"')}
          ${field("off_above", "Aus, wenn alle über (°C)", num(a.off_above), 'inputmode="decimal"')}
          ${field("days", "Tage (ab heute)", a.days ?? 6, 'inputmode="numeric"')}
          <label class="sfield"><span>Wert je Tag</span><select data-sa-field="basis">
            ${Object.entries(basisLabel).map(([k, l]) => `<option value="${k}" ${k === a.basis ? "selected" : ""}>${l}</option>`).join("")}
          </select></label>
        </div>
        <div class="hint">Geprüft täglich um 6:05 und 18:05 und beim Start. Geschaltet wird nur, wenn sich das Ergebnis ändert – wer von Hand umschaltet, wird nicht gleich wieder überstimmt.</div>`;
    } else {
      detail = `<div class="hint">Die Heizperiode wird nur von Hand geschaltet (hier, im Dashboard oder am S7).</div>`;
    }
    let statusHtml = "";
    if (!this._seasonDraft && st.checked && st.mode === a.mode && a.mode !== "manual") {
      const t = new Date(st.checked).toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" });
      const vals = Array.isArray(st.values) && st.values.length
        ? `<div class="svals">${st.values.map((v) => `<span style="background:${tempColor(v)}">${fmtT(v)}°</span>`).join("")}</div>` : "";
      statusHtml = `<div class="sstatus"><div class="lbl">Letzte Prüfung ${esc(t)}</div>${vals}<div>${esc(st.text || "")}</div></div>`;
    }
    return `
      <div class="season">
        <div class="srow">
          <div><div class="lbl">Heizperiode</div><div class="big">${on ? "An" : "Aus"}</div></div>
          ${ent
            ? `<div class="mode"><button class="seg ${on ? "on" : ""}" data-season-set="1">An</button><button class="seg ${!on ? "on" : ""}" data-season-set="0">Aus</button></div>`
            : `<span class="muted">Keine Heizperiode-Entität eingestellt</span>`}
        </div>
        <div class="lbl">Automatisch schalten</div>
        <div class="chips">${[["manual", "Manuell"], ["dates", "Nach Datum"], ["weather", "Nach Wetter"]]
          .map(([k, l]) => `<button class="chip ${a.mode === k ? "on" : ""}" data-sa-mode="${k}">${l}</button>`).join("")}</div>
        ${detail}
        ${a.mode !== "manual" || this._seasonDraft ? `<div class="dact"><button class="txt" data-sa-cancel>Verwerfen</button><button class="txt primary" data-sa-save>Speichern</button></div>` : ""}
        ${statusHtml}
      </div>`;
  }

  _renderPopup() {
    const { room: roomId, day, idx } = this._popup;
    const room = this._roomById(roomId);
    if (!room) return "";
    const b = dayBlocks(room.plan[day])[idx];
    if (!b) return "";
    const v = this._popup.value ?? b.value;
    const sw = isSwitch(room);
    const valueField = sw
      ? `<div class="field big-temp">
           <button class="seg ${v ? "on" : ""}" data-set-on="1">An</button>
           <button class="seg ${!v ? "on" : ""}" data-set-on="0">Aus</button>
         </div>`
      : `<div class="field big-temp">
           <button class="mini" data-t="-0.5">−</button>
           <span class="tv" style="color:${tempColor(v)}">${fmtT(v)} °C</span>
           <button class="mini" data-t="0.5">+</button>
         </div>`;
    return `
      <div class="overlay" data-close>
        <div class="dialog" data-stop>
          <div class="dtitle">${esc(room.name)} · ${DAY_LONG[day]}</div>
          <div class="field"><label>Von</label>
            ${idx === 0 ? `<span>00:00</span>` : `<input type="time" step="900" data-f="start" value="${this._popup.startVal || fmtMin(b.start)}">`}
            <label>bis</label><span>${b.end === 1440 ? "24:00" : fmtMin(b.end)}</span></div>
          ${valueField}
          <div class="field"><label>Neuer Block ab</label><input type="time" step="900" data-f="split" value="${this._popup.splitVal || ""}"></div>
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
    const { day, room: roomId } = this._copy;
    const room = this._roomById(roomId);
    return `
      <div class="overlay" data-close>
        <div class="dialog" data-stop>
          <div class="dtitle">${esc(room?.name || "")}: ${DAY_LONG[day]} kopieren nach</div>
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
    requestAnimationFrame(() => $(".blk span").forEach((sp) => {
      if (sp.parentElement.clientWidth < sp.scrollWidth + 6 && sp.dataset.short) sp.textContent = sp.dataset.short;
      if (sp.parentElement.clientWidth < sp.scrollWidth + 6) sp.style.visibility = "hidden";
    }));
    $("[data-tab]").forEach((el) => el.addEventListener("click", () => { this._tab = el.dataset.tab; this._render(); }));
    $("[data-season-set]").forEach((el) => el.addEventListener("click", () => {
      const ent = this._data.season_entity;
      if (!ent) return;
      this._hass.callService(ent.split(".")[0], el.dataset.seasonSet === "1" ? "turn_on" : "turn_off", { entity_id: ent });
    }));
    $("[data-sa-mode]").forEach((el) => el.addEventListener("click", () => {
      const base = this._seasonDraft || { ...(this._data.season_auto || {}) };
      base.mode = el.dataset.saMode;
      if (base.mode === "weather" && !base.weather_entity) {
        base.weather_entity = Object.keys(this._hass.states).filter((e) => e.startsWith("weather.")).sort()[0] || null;
      }
      this._seasonDraft = base;
      this._render();
    }));
    $("[data-sa-field]").forEach((el) => {
      const upd = () => {
        if (!this._seasonDraft) this._seasonDraft = { ...(this._data.season_auto || {}) };
        this._seasonDraft[el.dataset.saField] = el.value;
      };
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", upd);
    });
    $("[data-sa-cancel]").forEach((el) => el.addEventListener("click", () => { this._seasonDraft = null; this._render(); }));
    $("[data-sa-save]").forEach((el) => el.addEventListener("click", () => {
      const d = this._seasonDraft || this._data.season_auto || {};
      const n = (v) => Number(String(v).replace(",", "."));
      const payload = { type: "heizplan/set_season_auto", mode: d.mode, on_date: String(d.on_date ?? ""), off_date: String(d.off_date ?? ""),
        weather_entity: d.weather_entity || null, on_below: n(d.on_below), off_above: n(d.off_above), days: Math.round(n(d.days)), basis: d.basis };
      this._call(payload).then(() => { this._seasonDraft = null; this._render(); }).catch(() => {});
    }));
    $("[data-mode]").forEach((el) => el.addEventListener("click", () => this._call({ type: "heizplan/set_mode", mode: el.dataset.mode })));
    $("[data-away]").forEach((el) => el.addEventListener("click", () =>
      this._call({ type: "heizplan/set_away_temp", temp: this._data.away_temp + Number(el.dataset.away) })));
    $("[data-open]").forEach((el) => el.addEventListener("click", () => { this._room = el.dataset.open; this._tab = "week"; this._render(); }));
    $("[data-room-chip]").forEach((el) => el.addEventListener("click", () => { this._room = el.dataset.roomChip; this._render(); }));
    $("[data-daychip]").forEach((el) => el.addEventListener("click", () => { this._day = el.dataset.daychip; this._render(); }));
    $("[data-ovr]").forEach((el) => el.addEventListener("click", () => {
      const room = this._data.rooms[el.dataset.ovr];
      const base = room.status.target ?? room.status.plan_temp;
      this._call({ type: "heizplan/set_override", room_id: el.dataset.ovr, temp: base + Number(el.dataset.delta), minutes: 120 });
    }));
    $("[data-ovr-on]").forEach((el) => el.addEventListener("click", () =>
      this._call({ type: "heizplan/set_override", room_id: el.dataset.ovrOn, on: el.dataset.val === "1", minutes: 120 })));
    $("[data-boost]").forEach((el) => el.addEventListener("click", () =>
      this._call({ type: "heizplan/set_boost", room_id: el.dataset.boost, on: el.dataset.on === "1" }).catch(() => {})));
    $("[data-clear]").forEach((el) => el.addEventListener("click", () => this._call({ type: "heizplan/clear_override", room_id: el.dataset.clear })));

    // Wochenplan
    $(".blk.click").forEach((el) => el.addEventListener("click", (ev) => {
      if (this._justDragged) return;
      ev.stopPropagation();
      this._popup = { room: el.dataset.room, day: el.dataset.day, idx: Number(el.dataset.idx) };
      this._render();
    }));
    $(".handle").forEach((el) => el.addEventListener("pointerdown", (ev) => this._startDrag(ev, el)));
    $("[data-copy]").forEach((el) => el.addEventListener("click", () => { this._copy = { day: el.dataset.copy, room: el.dataset.copyRoom }; this._render(); }));
    $("[data-save-tpl]").forEach((el) => el.addEventListener("click", () => {
      const room = this._data.rooms[el.dataset.saveTpl];
      const name = prompt("Name der Vorlage:", `${room.name}`);
      if (name) this._call({ type: "heizplan/save_template", name, plan: room.plan, kind: room.kind || "climate" });
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
    const keepInputs = () => {
      this._popup.startVal = this.shadowRoot.querySelector('[data-f="start"]')?.value;
      this._popup.splitVal = this.shadowRoot.querySelector('[data-f="split"]')?.value;
    };
    $("[data-t]").forEach((el) => el.addEventListener("click", () => {
      const room = this._roomById(this._popup.room);
      const b = dayBlocks(room.plan[this._popup.day])[this._popup.idx];
      keepInputs();
      this._popup.value = clamp((this._popup.value ?? b.value) + Number(el.dataset.t), 5, 30);
      this._render();
    }));
    $("[data-set-on]").forEach((el) => el.addEventListener("click", () => {
      keepInputs();
      this._popup.value = el.dataset.setOn === "1";
      this._render();
    }));
    $("[data-save-block]").forEach((el) => el.addEventListener("click", () => this._saveBlock()));
    $("[data-del-block]").forEach((el) => el.addEventListener("click", () => this._deleteBlock()));
    $("[data-pick]").forEach((el) => el.addEventListener("click", () => {
      const pick = el.dataset.pick.split(",");
      this.shadowRoot.querySelectorAll("[data-copy-day]").forEach((c) => { c.checked = pick.includes(c.value); });
    }));
    $("[data-do-copy]").forEach((el) => el.addEventListener("click", () => {
      const room = this._roomById(this._copy.room);
      const day = this._copy.day;
      const to = [...this.shadowRoot.querySelectorAll("[data-copy-day]:checked")].map((c) => c.value);
      this._copy = null;
      if (!to.length || !room) { this._render(); return; }
      this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks: room.plan[day], copy_to: to });
    }));
  }

  _setValue(block, value, sw) {
    if (sw) { block.on = Boolean(value); delete block.temp; } else { block.temp = value; delete block.on; }
  }

  _saveBlock() {
    const room = this._roomById(this._popup.room);
    const sw = isSwitch(room);
    const { day, idx } = this._popup;
    const blocks = room.plan[day].map((b) => ({ ...b }));
    const block = blocks[idx];
    this._setValue(block, this._popup.value ?? blockValue(block), sw);
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
        const host = dayBlocks(blocks).find((b) => b.start < m && m < b.end) || dayBlocks(blocks)[0];
        const nb = { start: fmtMin(m) };
        // neuer Block: Gegenteil (An/Aus) bzw. +1 °C als Startwert
        this._setValue(nb, sw ? !host.value : clamp(host.value + 1, 5, 30), sw);
        blocks.push(nb);
        blocks.sort((a, b) => toMin(a.start) - toMin(b.start));
      }
    }
    this._popup = null;
    this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks });
  }

  _deleteBlock() {
    const room = this._roomById(this._popup.room);
    const { day, idx } = this._popup;
    const blocks = room.plan[day].filter((_, i) => i !== idx);
    this._popup = null;
    this._call({ type: "heizplan/set_day", room_id: room.id, day, blocks });
  }

  _startDrag(ev, handle) {
    ev.preventDefault();
    const bar = handle.parentElement;
    const room = this._roomById(handle.dataset.room);
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
  .tright { display:flex; gap:6px; align-items:center; min-width:0; }
  .aux { font-size:.72em; padding:2px 7px; border-radius:10px; white-space:nowrap; background: rgba(127,127,127,.15); display:inline-flex; align-items:center; gap:3px; }
  .aux.on { background: rgba(232,89,12,.18); color:#c2410c; font-weight:600; }
  .aux.wait { background: rgba(3,169,244,.15); }
  .boost { align-self:flex-start; display:inline-flex; align-items:center; gap:5px; border:1px dashed rgba(232,89,12,.6); background:none;
    color:#c2410c; border-radius:14px; padding:4px 10px; font:inherit; font-size:.82em; cursor:pointer; }
  .boost ha-icon { --mdc-icon-size:16px; width:16px; height:16px; }
  .boost.on { border-style:solid; background: rgba(232,89,12,.15); font-weight:600; }
  .boost[disabled] { opacity:.4; cursor:default; }
  .aux ha-icon { --mdc-icon-size:14px; width:14px; height:14px; }
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
  .warn { font-size:.8em; padding:4px 8px; border-radius:6px; background: rgba(255,152,0,.18); }
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
  .rname { width:110px; flex:none; text-align:left; font-size:.9em; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .chips.days { margin-top:-4px; }
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
  @media (max-width: 500px) { .rname { width:80px; } .week.all .scale > span:first-child { width:80px !important; } .ticks span:nth-child(even) { display:none; } .temps { gap:12px; } }
  .season { display:flex; flex-direction:column; gap:10px; }
  .srow { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  .sgrid { display:grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap:10px; }
  .sfield { display:flex; flex-direction:column; gap:4px; font-size:.85em; color:var(--secondary-text-color); }
  .sfield.wide { grid-column: 1 / -1; }
  .sfield input, .sfield select { font:inherit; font-size:1.05em; color:var(--primary-text-color); padding:8px; border-radius:8px;
    border:1px solid var(--divider-color); background:var(--card-background-color); min-width:0; }
  .sstatus { border:1px solid var(--divider-color); border-radius:10px; padding:8px 10px; display:flex; flex-direction:column; gap:6px; font-size:.9em; }
  .svals { display:flex; gap:4px; flex-wrap:wrap; }
  .svals span { color:#fff; font-weight:600; font-size:.85em; padding:2px 6px; border-radius:6px; text-shadow:0 0 2px rgba(0,0,0,.4); }
  .banner[data-tab] { cursor:pointer; }
  .blink { color: var(--primary-color); white-space:nowrap; }
  /* Handy / schmale Karte: kompakte Räume, Auswahl-Leisten wischen statt umbrechen */
  :host { display:block; container-type: inline-size; }
  @container (max-width: 520px) {
    ha-card { padding: 10px 10px 12px; }
    .head { gap:6px; }
    .title { font-size: 1.15em; }
    .mode { flex-wrap:nowrap; gap:4px; }
    .seg { padding:5px 10px; }
    .away { margin-left:2px; white-space:nowrap; }
    .mini { width:34px; height:34px; }
    .tabs, .chips { flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; -webkit-overflow-scrolling:touch; margin-left:-10px; margin-right:-10px; padding:0 10px; }
    .tabs::-webkit-scrollbar, .chips::-webkit-scrollbar { display:none; }
    .tab, .chip, .seg { white-space:nowrap; flex:none; }
    .grid { grid-template-columns: 1fr; gap: 8px; }
    .tile { display:grid; grid-template-columns: 1fr auto; column-gap:8px; row-gap:6px; padding:8px 10px; }
    .tile > .trow, .tile > .bar, .tile > .warn, .tile > .next, .tile > .boost { grid-column: 1 / -1; }
    .tile > .temps { grid-column: 1; gap:14px; }
    .tile > .actions { grid-column: 2; grid-row: 2; align-self:end; }
    .tile.switch > .actions { flex-direction:column; align-items:flex-end; gap:0; }
    .tile .hint { display:none; }
    .tile .big { font-size:1.35em; }
    .tile .mid { font-size:1em; }
    .tile .next:empty { display:none; }
  }
`;

if (!customElements.get("heizplan-card")) {
  customElements.define("heizplan-card", HeizplanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "heizplan-card", name: "Heizplan", description: "Wochenpläne, Soll/Ist je Raum, Vorlagen", preview: false });
  console.info(`%c HEIZPLAN %c ${HP_VERSION} `, "background:#e8590c;color:#fff", "");
}
