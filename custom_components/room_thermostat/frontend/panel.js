const DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
const SHORT_DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const time = (minute) => `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
const freshSchedule = () => Array.from({ length: 7 }, () => [{ minute: 0, temperature: 20 }]);

class RoomThermostatPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this.data = { rooms: [], status: {}, settings: { outdoor_entity: "", heating_gate_enabled: true, heating_gate_hours: 4, heating_gate_on: 16, heating_gate_off: 18, valve_exercise_enabled: true }, heating_gate: {}, valve_exercise: {} };
    this.selected = null;
    this.day = (new Date().getDay() + 6) % 7;
    this.draft = null;
    this.editing = false;
    this.settingsOpen = false;
    this.settingsDraft = null;
    this.busy = false;
    this.error = "";
    this.lastFetch = 0;
    this.timer = null;
    this.drag = null;
    this.roomDrag = null;
    this.suppressClick = false;
    this.shadowRoot.addEventListener("click", (event) => this.onClick(event));
    this.shadowRoot.addEventListener("change", (event) => this.onChange(event));
    this.shadowRoot.addEventListener("input", (event) => this.onEntitySearch(event));
    this.shadowRoot.addEventListener("focusin", (event) => this.onEntityFocus(event));
    this.shadowRoot.addEventListener("keydown", (event) => this.onEntityKeydown(event));
    this.shadowRoot.addEventListener("pointerdown", (event) => this.onPointerDown(event));
    this.addEventListener("pointermove", (event) => this.onPointerMove(event));
    this.addEventListener("pointerup", (event) => this.onPointerUp(event));
    this.addEventListener("pointercancel", (event) => this.onPointerUp(event));
  }

  set hass(value) {
    this._hass = value;
    if (value && !this.lastFetch) this.load();
  }
  get hass() { return this._hass; }

  connectedCallback() {
    this.render();
    this.timer = setInterval(() => { if (!this.editing) this.load(); }, 15000);
  }
  disconnectedCallback() { clearInterval(this.timer); }

  async load() {
    if (!this._hass || this.busy || this.roomDrag) return;
    this.lastFetch = Date.now();
    try {
      const data = await this._hass.connection.sendMessagePromise({ type: "room_thermostat/rooms" });
      if (this.roomDrag || this.busy) return;
      this.data = data;
      if (!this.selected || !this.data.rooms.some((r) => r.id === this.selected)) this.selected = this.data.rooms[0]?.id || null;
      this.error = "";
    } catch (err) { if (this.roomDrag || this.busy) return; this.error = this.message(err); }
    this.render();
  }

  message(err) { return err?.message || err?.error?.message || String(err); }
  room() { return this.data.rooms.find((r) => r.id === this.selected); }
  status(room) { return this.data.status?.[room.id] || {}; }
  fmt(value, digits = 1) {
    if (!Number.isFinite(value)) return "—";
    const rounded = Number(value).toFixed(digits);
    return (Number(rounded) === 0 ? (0).toFixed(digits) : rounded).replace(".", ",");
  }
  stateLabel(status) {
    return status.state === "active" ? "Auto" : status.state === "exercise" ? "Ventilschutz" : status.state === "window" ? "Lüften erkannt" : status.state === "manual" ? "Manuell" : status.state === "blocked" ? "Außen-Sperre" : status.state === "error" ? "Prüfen" : "Deaktiviert";
  }

  newRoom() {
    this.settingsOpen = false;
    this.draft = { name: "", actual_entity: "", cold_offset: 1, warm_offset: -1, minimum_valve: 0, valve_entity: "", schedule: freshSchedule(), enabled: true };
    this.day = 0;
    this.editing = true;
    this.error = "";
    this.render();
  }

  async onClick(event) {
    if (this.suppressClick) { this.suppressClick = false; return; }
    if (event.target.closest("[data-entity-option]")) {
      const option = event.target.closest("[data-entity-option]");
      (this.settingsOpen ? this.settingsDraft : this.draft)[option.dataset.field] = option.dataset.entity;
      this.error = "";
      this.render();
      return;
    }
    if (!event.target.closest(".entity-picker")) this.closeEntityLists();
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "home") {
      this.settingsOpen = false; this.settingsDraft = null;
      this.editing = false; this.draft = null; this.error = "";
      this.render(); return;
    }
    if (action === "settings") {
      this.settingsDraft = { outdoor_entity: this.data.settings?.outdoor_entity || "", heating_gate_enabled: this.data.settings?.heating_gate_enabled !== false, heating_gate_hours: this.data.settings?.heating_gate_hours ?? 4, heating_gate_on: this.data.settings?.heating_gate_on ?? 16, heating_gate_off: this.data.settings?.heating_gate_off ?? 18, valve_exercise_enabled: this.data.settings?.valve_exercise_enabled !== false };
      this.settingsOpen = true; this.editing = false; this.error = ""; this.render(); return;
    }
    if (action === "settings-cancel") { this.settingsOpen = false; this.settingsDraft = null; this.error = ""; this.render(); return; }
    if (action === "settings-save") { await this.saveSettings(); return; }
    if (action === "new") this.newRoom();
    if (action === "edit") {
      this.settingsOpen = false;
      this.selected = button.dataset.id;
      this.draft = structuredClone(this.room());
      this.editing = true;
      this.day = 0;
      this.error = "";
      this.render();
    }
    if (action === "temperature-info") {
      this.dispatchEvent(new CustomEvent("hass-more-info", {
        detail: { entityId: button.dataset.entity }, bubbles: true, composed: true,
      }));
    }
    if (action === "more-info") {
      try {
        await this._hass.connection.sendMessagePromise({
          type: "room_thermostat/manual_prepare", room_id: button.dataset.id,
        });
        this.error = "";
      } catch (err) { this.error = this.message(err); this.render(); }
      this.dispatchEvent(new CustomEvent("hass-more-info", {
        detail: { entityId: button.dataset.entity }, bubbles: true, composed: true,
      }));
    }
    if (action === "stop-manual") {
      try {
        this.data = await this._hass.connection.sendMessagePromise({
          type: "room_thermostat/manual_stop", room_id: button.dataset.id,
        });
        this.error = "";
      } catch (err) { this.error = this.message(err); }
      this.render();
    }
    if (action === "cancel") { this.editing = false; this.draft = null; this.error = ""; this.render(); }
    if (action === "day") { this.day = Number(button.dataset.day); this.render(); }
    if (action === "graph-add") {
      const rect = button.getBoundingClientRect();
      const minute = Math.max(0, Math.min(1439, Math.round(((event.clientX - rect.left - 13) / (rect.width - 26)) * 1440)));
      const slots = this.draft.schedule[this.day];
      if (slots.length >= 48) { this.error = "Pro Tag sind höchstens 48 Schaltpunkte möglich."; this.render(); return; }
      if (slots.some((slot) => slot.minute === minute)) return;
      let temperature = slots.filter((slot) => slot.minute <= minute).at(-1)?.temperature;
      if (temperature === undefined) {
        for (let distance = 1; distance <= 7; distance++) {
          const previous = this.draft.schedule[(this.day - distance + 7) % 7];
          if (previous.length) { temperature = previous.at(-1).temperature; break; }
        }
      }
      slots.push({ minute, temperature: temperature ?? 20 });
      slots.sort((a, b) => a.minute - b.minute);
      this.render();
    }
    if (action === "add-slot") {
      if (this.draft.schedule[this.day].length >= 48) { this.error = "Pro Tag sind höchstens 48 Schaltpunkte möglich."; this.render(); return; }
      const existing = new Set(this.draft.schedule[this.day].map((s) => s.minute));
      const candidate = [360, 480, 720, 1020, 1320, 0].find((m) => !existing.has(m));
      if (candidate === undefined) { this.error = "Bitte zuerst einen Schaltpunkt entfernen."; this.render(); return; }
      const current = this.draft.schedule[this.day].filter((s) => s.minute <= candidate).at(-1);
      this.draft.schedule[this.day].push({ minute: candidate, temperature: current?.temperature ?? 20 });
      this.draft.schedule[this.day].sort((a, b) => a.minute - b.minute);
      this.render();
    }
    if (action === "remove-slot") {
      this.draft.schedule[this.day].splice(Number(button.dataset.index), 1);
      this.render();
    }
    if (action === "copy-day") {
      if (!confirm(`${DAYS[this.day]} auf alle anderen Tage kopieren?`)) return;
      const source = this.draft.schedule[this.day];
      this.draft.schedule = this.draft.schedule.map((day, index) => index === this.day ? day : structuredClone(source));
      this.render();
    }
    if (action === "save") await this.save();
    if (action === "delete") await this.deleteRoom();
  }

  onChange(event) {
    const el = event.target;
    if (this.settingsOpen && el.dataset.field) {
      this.settingsDraft[el.dataset.field] = ["heating_gate_enabled", "valve_exercise_enabled"].includes(el.dataset.field) ? el.checked : el.dataset.field === "outdoor_entity" ? el.value : Number(el.value);
      this.error = "";
      return;
    }
    if (!this.draft || !el.dataset.field) return;
    if (el.dataset.field === "enabled") this.draft.enabled = el.checked;
    else if (el.dataset.field === "slot-time") {
      const [hours, minutes] = el.value.split(":").map(Number);
      this.draft.schedule[this.day][Number(el.dataset.index)].minute = hours * 60 + minutes;
      this.draft.schedule[this.day].sort((a, b) => a.minute - b.minute);
    } else if (el.dataset.field === "slot-temperature") {
      this.draft.schedule[this.day][Number(el.dataset.index)].temperature = Number(el.value);
    } else if (["cold_offset", "warm_offset", "minimum_valve"].includes(el.dataset.field)) {
      this.draft[el.dataset.field] = Number(el.value);
    } else this.draft[el.dataset.field] = el.value;
    this.error = "";
    this.render();
  }

  entityStates(domain) {
    return Object.values(this._hass?.states || {})
      .filter((state) => state.entity_id.startsWith(`${domain}.`))
      .sort((a, b) => (a.attributes.friendly_name || a.entity_id).localeCompare(b.attributes.friendly_name || b.entity_id, "de"));
  }

  entityPicker(field, domain, selected, optional = false) {
    const state = this._hass?.states?.[selected];
    const label = selected ? `${state?.attributes?.friendly_name || selected} · ${selected}` : "";
    return `<div class="entity-picker" data-picker="${field}"><input type="search" data-entity-search="${field}" data-domain="${domain}" data-optional="${optional}" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" placeholder="Entität suchen …" value="${esc(label)}"><div class="entity-results" role="listbox" hidden></div></div>`;
  }

  closeEntityLists(except = null) {
    this.shadowRoot.querySelectorAll(".entity-picker").forEach((picker) => {
      if (picker === except) return;
      picker.querySelector(".entity-results").hidden = true;
      picker.querySelector("[data-entity-search]").setAttribute("aria-expanded", "false");
    });
  }

  updateEntityResults(input) {
    const picker = input.closest(".entity-picker");
    this.closeEntityLists(picker);
    const query = input.value.trim().toLocaleLowerCase("de");
    const matches = this.entityStates(input.dataset.domain).filter((state) =>
      `${state.attributes.friendly_name || ""} ${state.entity_id}`.toLocaleLowerCase("de").includes(query)
    ).slice(0, 50);
    const clear = input.dataset.optional === "true" ? `<button type="button" role="option" data-entity-option data-field="${input.dataset.entitySearch}" data-entity=""><strong>Keine Korrektur</strong><small>Außentemperatur nicht verwenden</small></button>` : "";
    const results = picker.querySelector(".entity-results");
    results.innerHTML = clear + matches.map((state) => `<button type="button" role="option" data-entity-option data-field="${input.dataset.entitySearch}" data-entity="${esc(state.entity_id)}"><strong>${esc(state.attributes.friendly_name || state.entity_id)}</strong><small>${esc(state.entity_id)}</small></button>`).join("") || `<div class="entity-empty">Keine passende Entität gefunden</div>`;
    results.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  onEntityFocus(event) {
    if (event.target.matches("[data-entity-search]")) this.updateEntityResults(event.target);
  }

  onEntitySearch(event) {
    if (!event.target.matches("[data-entity-search]")) return;
    (this.settingsOpen ? this.settingsDraft : this.draft)[event.target.dataset.entitySearch] = "";
    this.updateEntityResults(event.target);
  }

  onEntityKeydown(event) {
    const handle = event.target.closest("[data-room-handle]");
    if (handle && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      event.preventDefault();
      this.moveRoom(handle.dataset.roomHandle, event.key === "ArrowUp" ? -1 : 1);
      return;
    }
    if (!event.target.matches("[data-entity-search]")) return;
    if (event.key === "Escape") { this.closeEntityLists(); event.target.blur(); }
    if (event.key === "Enter" || event.key === "ArrowDown") {
      const first = event.target.closest(".entity-picker").querySelector(".entity-results button");
      if (first) { event.preventDefault(); first.focus(); if (event.key === "Enter") first.click(); }
    }
  }

  onPointerDown(event) {
    const handle = event.target.closest("[data-room-handle]");
    if (handle && !this.editing && !this.settingsOpen && !this.busy && this.data.rooms.length > 1) {
      event.preventDefault();
      this.roomDrag = { pointerId: event.pointerId, card: handle.closest(".room-card"), startY: event.clientY, moved: false };
      this.setPointerCapture(event.pointerId);
      return;
    }
    if (!this.editing) return;
    const segment = event.target.closest("[data-segment]");
    if (!segment) return;
    event.preventDefault();
    const index = Number(segment.dataset.segment);
    const slot = this.draft.schedule[this.day][index];
    if (!slot) return;
    const svg = segment.closest("svg");
    const geometry = this.graphGeometry(this.draft.schedule, this.day);
    const height = svg.getBoundingClientRect().height;
    this.drag = { pointerId: event.pointerId, day: this.day, index, startY: event.clientY, startTemp: slot.temperature,
      low: geometry.low, high: geometry.high, height, moved: false };
    this.setPointerCapture(event.pointerId);
  }

  onPointerMove(event) {
    if (this.roomDrag && event.pointerId === this.roomDrag.pointerId) {
      const drag = this.roomDrag;
      if (!drag.moved && Math.abs(event.clientY - drag.startY) < 6) return;
      drag.moved = true;
      drag.card.classList.add("room-dragging");
      for (const card of this.shadowRoot.querySelectorAll(".overview-grid > .room-card")) {
        if (card === drag.card) continue;
        const rect = card.getBoundingClientRect();
        if (event.clientY >= rect.top && event.clientY <= rect.bottom) {
          if (event.clientY < rect.top + rect.height / 2) card.before(drag.card);
          else card.after(drag.card);
          break;
        }
      }
      return;
    }
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const delta = event.clientY - this.drag.startY;
    if (Math.abs(delta) < 3 && !this.drag.moved) return;
    const degrees = delta * (this.drag.high - this.drag.low) * 140 / (92 * this.drag.height);
    const value = Math.max(5, Math.min(35, Math.round((this.drag.startTemp - degrees) * 10) / 10));
    const slot = this.draft.schedule[this.drag.day][this.drag.index];
    if (slot && slot.temperature !== value) {
      slot.temperature = value;
      this.drag.moved = true;
      this.suppressClick = true;
      this.redrawGraph();
      const input = this.shadowRoot.querySelector(`.slot [data-field="slot-temperature"][data-index="${this.drag.index}"]`);
      if (input) input.value = value;
    }
  }

  onPointerUp(event) {
    if (this.roomDrag && event.pointerId === this.roomDrag.pointerId) {
      const drag = this.roomDrag;
      if (this.hasPointerCapture(event.pointerId)) this.releasePointerCapture(event.pointerId);
      this.roomDrag = null;
      drag.card.classList.remove("room-dragging");
      if (drag.moved) {
        this.suppressClick = true;
        setTimeout(() => { this.suppressClick = false; }, 100);
        if (event.type === "pointercancel") this.render();
        else this.saveRoomOrder([...this.shadowRoot.querySelectorAll(".overview-grid > .room-card")].map((card) => card.dataset.roomId));
      }
      return;
    }
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    if (this.hasPointerCapture(event.pointerId)) this.releasePointerCapture(event.pointerId);
    const moved = this.drag.moved;
    this.drag = null;
    if (moved) this.render();
    setTimeout(() => { this.suppressClick = false; }, 100);
  }

  redrawGraph() {
    const svg = this.shadowRoot.querySelector(".graph.interactive svg");
    if (!svg || !this.drag) return;
    const geometry = this.graphGeometry(this.draft.schedule, this.drag.day, this.drag);
    svg.querySelector("[data-graph-area]").setAttribute("d", geometry.area);
    svg.querySelector("[data-graph-line]").setAttribute("d", geometry.path);
    svg.querySelectorAll("[data-graph-point]").forEach((point, index) => point.setAttribute("cy", geometry.y(geometry.points[index].temperature)));
    svg.querySelectorAll("[data-segment]").forEach((line, index) => {
      const y = geometry.y(geometry.points[index].temperature);
      line.setAttribute("y1", y);
      line.setAttribute("y2", y);
    });
  }

  moveRoom(roomId, direction) {
    if (this.busy) return;
    const ids = this.data.rooms.map((room) => room.id);
    const index = ids.indexOf(roomId);
    const next = index + direction;
    if (index < 0 || next < 0 || next >= ids.length) return;
    [ids[index], ids[next]] = [ids[next], ids[index]];
    this.saveRoomOrder(ids, roomId);
  }

  async saveRoomOrder(ids, focusRoomId = null) {
    if (this.busy || ids.length !== this.data.rooms.length) return;
    const previous = this.data;
    const byId = new Map(previous.rooms.map((room) => [room.id, room]));
    this.data = { ...previous, rooms: ids.map((id) => byId.get(id)) };
    this.busy = true;
    try {
      this.data = await this._hass.connection.sendMessagePromise({ type: "room_thermostat/reorder_rooms", room_ids: ids });
      this.error = "";
    } catch (err) { this.data = previous; this.error = this.message(err); }
    this.busy = false;
    this.render();
    if (focusRoomId) {
      [...this.shadowRoot.querySelectorAll("[data-room-handle]")]
        .find((handle) => handle.dataset.roomHandle === focusRoomId)?.focus();
    }
  }

  async saveSettings() {
    if (this.busy) return;
    this.busy = true; this.render();
    try {
      this.data = await this._hass.connection.sendMessagePromise({ type: "room_thermostat/save_settings", settings: this.settingsDraft });
      this.settingsOpen = false; this.settingsDraft = null; this.error = "";
    } catch (err) { this.error = this.message(err); }
    this.busy = false; this.render();
  }

  async save() {
    if (this.busy) return;
    const room = this.draft;
    if (!room.name.trim() || !room.actual_entity || !room.valve_entity) { this.error = "Bitte Name, Ist-Wert und Ventil-Ziel wählen."; this.render(); return; }
    if (!room.schedule.some((day) => day.length)) { this.error = "Mindestens ein Schaltpunkt ist nötig."; this.render(); return; }
    for (const day of room.schedule) {
      if (new Set(day.map((s) => s.minute)).size !== day.length) { this.error = "Zwei Schaltpunkte am selben Tag haben dieselbe Uhrzeit."; this.render(); return; }
    }
    this.busy = true; this.render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "room_thermostat/save_room", room });
      this.data = result;
      this.selected = result.room.id;
      this.editing = false;
      this.draft = null;
      this.error = "";
    } catch (err) { this.error = this.message(err); }
    this.busy = false; this.render();
  }

  async deleteRoom() {
    if (this.busy || !this.draft?.id) return;
    if (!confirm(`Raum „${this.draft.name}“ wirklich löschen? Die letzte Ventilstellung bleibt bestehen.`)) return;
    this.busy = true;
    try {
      this.data = await this._hass.connection.sendMessagePromise({ type: "room_thermostat/delete_room", room_id: this.draft.id });
      this.selected = this.data.rooms[0]?.id || null;
      this.editing = false; this.draft = null; this.error = "";
    } catch (err) { this.error = this.message(err); }
    this.busy = false; this.render();
  }

  graphGeometry(schedule, chosen, limits = null) {
    const day = schedule[chosen] || [];
    const points = [...day].sort((a, b) => a.minute - b.minute);
    let start = points[0].temperature;
    if (points[0].minute !== 0) {
      for (let distance = 1; distance <= 7; distance++) {
        const previous = schedule[(chosen - distance + 7) % 7];
        if (previous.length) { start = previous.at(-1).temperature; break; }
      }
    }
    const temperatures = [start, ...points.map((p) => p.temperature)];
    const low = limits?.low ?? Math.min(...temperatures) - 2;
    const high = limits?.high ?? Math.max(...temperatures) + 2;
    const y = (v) => 114 - ((v - low) / (high - low)) * 92;
    let path = `M 0 ${y(start)}`;
    for (const point of points) {
      const x = (point.minute / 1440) * 1000;
      path += ` H ${x} V ${y(point.temperature)}`;
    }
    path += ` H 1000`;
    const area = `${path} V 128 H 0 Z`;
    return { points, low, high, y, path, area };
  }

  graph(schedule, chosen, interactive = false) {
    const day = schedule[chosen] || [];
    if (!day.length) return `<div class="graph empty ${interactive ? "interactive" : ""}" ${interactive ? 'data-action="graph-add"' : ""}>Für diesen Tag gilt der letzte Schaltpunkt des Vortags.</div>`;
    const { points, y, path, area } = this.graphGeometry(schedule, chosen);
    return `<div class="graph ${interactive ? "interactive" : ""}" ${interactive ? 'data-action="graph-add" title="Zum Hinzufügen eines Schaltpunkts klicken"' : ""}><svg viewBox="0 0 1000 140" preserveAspectRatio="none" role="img" aria-label="Temperaturverlauf für ${DAYS[chosen]}">
      <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#82b6ff" stop-opacity=".43"/><stop offset="1" stop-color="#82b6ff" stop-opacity=".03"/></linearGradient></defs>
      <path data-graph-area d="${area}" fill="url(#fill)"/><path data-graph-line d="${path}" fill="none" stroke="#70aaff" stroke-width="4" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
      ${points.map((p) => `<circle data-graph-point cx="${(p.minute / 1440) * 1000}" cy="${y(p.temperature)}" r="6" fill="#f7fbff" stroke="#70aaff" stroke-width="3" vector-effect="non-scaling-stroke"/>`).join("")}
      ${interactive ? points.map((p, i) => `<line data-segment="${i}" x1="${(p.minute / 1440) * 1000 + 5}" x2="${((points[i + 1]?.minute ?? 1440) / 1440) * 1000 - 5}" y1="${y(p.temperature)}" y2="${y(p.temperature)}" stroke="transparent" stroke-width="22" vector-effect="non-scaling-stroke"/>`).join("") : ""}
    </svg><div class="axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div></div>`;
  }

  nextTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return new Intl.DateTimeFormat("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  exerciseTime(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
  }

  exerciseLabel(roomId) {
    const item = this.data.valve_exercise?.[roomId] || {};
    if (item.state === "running") return `läuft · bis ${this.nextTime(item.until)}`;
    if (item.state === "restore_error") return "Rückstellung fehlgeschlagen";
    if (item.state === "disabled" || item.state === "room_disabled") return "deaktiviert";
    if (item.state === "unavailable") return "Ventil nicht verfügbar";
    if (item.last_result === "failed") return `Start fehlgeschlagen · erneut ${this.exerciseTime(item.next_run)}`;
    if (item.last_result === "unconfirmed") return `100 % nicht bestätigt · erneut ${this.exerciseTime(item.next_run)}`;
    if (item.last_result === "interrupted") return `unterbrochen · erneut ${this.exerciseTime(item.next_run)}`;
    return item.next_run ? `nächste Fahrt ${this.exerciseTime(item.next_run)}` : "wird geplant";
  }

  render() {
    const dark = this._hass?.themes?.darkMode;
    this.shadowRoot.innerHTML = `<style>${CSS}</style><main class="${dark ? "dark" : "light"}">
      <div class="shell"><nav class="top-menu glass" aria-label="Hauptnavigation"><button type="button" class="top-menu-link ${!this.settingsOpen && !this.editing ? "active" : ""}" data-action="home" aria-label="Übersicht" title="Übersicht" ${!this.settingsOpen && !this.editing ? 'aria-current="page"' : ""}><span aria-hidden="true">⌂</span></button><button type="button" class="top-menu-link ${this.settingsOpen ? "active" : ""}" data-action="settings" aria-label="Einstellungen" title="Einstellungen" ${this.settingsOpen ? 'aria-current="page"' : ""}><span aria-hidden="true">⚙</span></button><button type="button" class="top-menu-add" data-action="new" aria-label="Raum hinzufügen" title="Raum hinzufügen"><span aria-hidden="true">＋</span></button></nav><div class="dashboard-surface glass"><header class="dashboard-header"><div class="dashboard-title"><div class="eyebrow">RAUMKLIMA · FUSSBODENHEIZUNG</div><h1>Engelsoft RoomControl</h1><p class="subtitle">Ruhige Wärme, Raum für Raum.</p></div>${!this.settingsOpen && !this.editing && this.data.rooms.length ? this.renderWeatherOverview() : ""}</header>
      ${this.error ? `<div class="alert" role="alert">${esc(this.error)}</div>` : ""}
      ${this.settingsOpen ? this.renderSettings() : this.editing ? this.renderEditor() : this.renderDashboard()}
      </div></div></main>`;
  }

  renderDashboard() {
    if (!this.data.rooms.length) return `<div class="welcome glass"><div class="welcome-icon">⌂</div><h2>Dein erster Raum</h2><p>Lege einen Raum an und verbinde Temperaturfühler und Ventil. Danach kannst du den Wochenplan minutengenau gestalten.</p><button class="primary" data-action="new">Raum anlegen</button></div>`;
    return `<section class="overview-grid" aria-label="Räume">${this.data.rooms.map((room) => this.renderRoomCard(room, this.status(room))).join("")}</section>`;
  }

  renderWeatherOverview() {
    const status = this.status(this.data.rooms[0]);
    const gate = this.data.heating_gate || {};
    const gateHours = this.data.settings?.heating_gate_hours ?? 4;
    const gateLabel = gate.reason === "blocked" ? "Gesperrt" : gate.reason === "disabled" ? "Deaktiviert" : gate.reason === "unavailable" ? "Freigegeben · Messwerte fehlen" : "Freigegeben";
    const weather = `<div class="weather-values"><span>Außen <b>${this.fmt(status.outdoor)} °C</b></span><span>24-h-Mittel <b>${this.fmt(status.outdoor_mean)} °C</b>${status.outdoor_coverage < 24 ? ` <em>(${this.fmt(status.outdoor_coverage)} h)</em>` : ""}</span><span>${this.fmt(gateHours, gateHours % 1 ? 1 : 0)}-h-Freigabewert <b>${this.fmt(gate.mean)} °C</b>${gate.coverage < gateHours ? ` <em>(${this.fmt(gate.coverage)} h)</em>` : ""}</span></div><div class="weather-gate ${gate.reason || "unavailable"}"><span>Heizfreigabe</span><strong>${gateLabel}</strong></div>`;
    return `<section class="weather-overview glass" aria-label="Außentemperatur und Heizfreigabe">${weather}</section>`;
  }

  renderSettings() {
    return `<div class="editor-head"><div class="eyebrow">ALLGEMEIN</div><h2>Zentrale Einstellungen</h2><p>Diese Werte gelten für alle Räume.</p></div>
      <section class="glass form-card central-settings"><label>Außentemperatur${this.entityPicker("outdoor_entity", "sensor", this.settingsDraft.outdoor_entity, true)}<small>Optional · der 24-Stunden-Mittelwert wird automatisch ermittelt.</small></label><label class="toggle-row"><span><strong>Heizfreigabe nach Außentemperatur</strong><small>Gilt für alle Räume. Fehlen Messwerte, bleibt Heizen freigegeben.</small></span><input type="checkbox" data-field="heating_gate_enabled" ${this.settingsDraft.heating_gate_enabled ? "checked" : ""}></label><div class="gate-settings"><label>Mittelung<input type="number" min="3" max="6" step="0.5" data-field="heating_gate_hours" value="${this.settingsDraft.heating_gate_hours}"><small>3 bis 6 Stunden</small></label><label>Heizen ein unter<input type="number" min="-20" max="35" step="0.5" data-field="heating_gate_on" value="${this.settingsDraft.heating_gate_on}"><small>°C Außentemperatur</small></label><label>Heizen aus über<input type="number" min="-20" max="35" step="0.5" data-field="heating_gate_off" value="${this.settingsDraft.heating_gate_off}"><small>°C · mindestens 0,5 °C über „ein“</small></label></div><label class="toggle-row"><span><strong>Wöchentlicher Ventilschutz</strong><small>Nach sieben Tagen ohne 100 % fährt jedes aktive Ventil für zehn Minuten auf. Starts erfolgen im Abstand von zwei Minuten.</small></span><input type="checkbox" data-field="valve_exercise_enabled" ${this.settingsDraft.valve_exercise_enabled ? "checked" : ""}></label></section>
      <div class="editor-actions"><button class="subtle" data-action="settings-cancel">Abbrechen</button><button class="primary" data-action="settings-save" ${this.busy ? "disabled" : ""}>${this.busy ? "Speichert …" : "Einstellungen speichern"}</button></div>`;
  }

  renderRoomCard(room, status) {
    const future = status.next_switches || [];
    const next = future[0];
    const physicalValve = status.valve_current;
    const valveOpening = Number.isFinite(physicalValve) ? Math.max(0, Math.min(100, physicalValve)) : 0;
    const delta = Number.isFinite(status.valve_30m) && Number.isFinite(status.valve) ? status.valve_30m - status.valve : 0;
    const trend = status.state === "manual" ? "Handsteuerung" : delta > 0.5 ? "öffnet langsam" : delta < -0.5 ? "schließt langsam" : "bleibt stabil";
    const preheat = status.preheat || {};
    const preheatStart = preheat.time && Number.isFinite(preheat.lead_hours)
      ? new Date(new Date(preheat.time).getTime() - preheat.lead_hours * 3600000) : null;
    const preheatLabel = preheat.active ? `jetzt · bis ${this.nextTime(preheat.time)}`
      : preheatStart ? `ab ca. ${this.nextTime(preheatStart)}` : "—";
    const windowState = !status.window_available ? "unknown" : status.window_open ? "alarm" : "ok";
    return `<article class="room-card glass" data-room-id="${esc(room.id)}"><div class="room-card-head"><div><h3>${esc(room.name)}</h3></div><div class="room-card-tools">${status.state === "manual" ? `<button class="state-chip manual manual-stop" data-action="stop-manual" data-id="${esc(room.id)}" aria-label="Manuelle Steuerung für ${esc(room.name)} beenden" title="Manuelle Steuerung beenden"><i></i>Manuell ×</button>` : `<span class="state-chip ${esc(status.state || "paused")}" title="${esc(status.message || "")}"><i></i>${esc(this.stateLabel(status))}</span>`}<span class="window-status ${windowState}" title="Fensteralarm: ${windowState === "alarm" ? "vermutlich offen" : windowState === "ok" ? "OK" : "unbekannt"}"><i></i>${windowState === "alarm" ? "Fenster offen?" : windowState === "ok" ? "Fenster OK" : "Fenster unbekannt"}</span><button class="gear" data-action="edit" data-id="${esc(room.id)}" aria-label="${esc(room.name)} konfigurieren" title="Konfigurieren">⚙</button><button class="room-order-handle" type="button" data-room-handle="${esc(room.id)}" aria-label="${esc(room.name)} verschieben" title="Ziehen oder mit Pfeiltasten verschieben">⠿</button></div></div>${status.state === "error" ? `<p class="room-card-error">${esc(status.message || "Werte derzeit nicht verfügbar")}</p>` : ""}
      <div class="room-card-body"><div class="room-summary"><div class="live-metrics"><button class="valve-tile ${Number.isFinite(physicalValve) && physicalValve > 0 ? "heating" : ""}" data-action="more-info" data-id="${esc(room.id)}" data-entity="${esc(room.valve_entity)}" title="Ventil-Dialog öffnen · bei Änderung 2 Stunden pausieren"><span>VENTIL ${status.state === "active" ? `<em class="valve-trend ${delta > 0.5 ? "opening" : delta < -0.5 ? "closing" : ""}">${esc(trend)}</em>` : ""}</span><strong>${this.fmt(physicalValve, 0)}<small> %</small></strong><span class="valve-progress" aria-hidden="true"><i style="width:${valveOpening}%"></i></span><span class="tile-detail">In 30 Min. ${this.fmt(status.valve_30m, 0)} %</span></button><button class="temperature-tile" data-action="temperature-info" data-entity="${esc(room.actual_entity)}" title="Ist-Temperatur in Home Assistant öffnen"><span>IST</span><strong>${this.fmt(status.actual)}<small> °C</small></strong><span class="tile-detail">Aufheiztempo ${status.heating_rate_count ? "· gelernt" : "· Startwert"} ${this.fmt(status.heating_rate, 2)} °C/h</span></button><button class="temperature-tile target-tile" data-action="temperature-info" data-entity="${esc(status.target_entity || "")}" title="Solltemperatur in Home Assistant öffnen"><span>SOLL</span><strong>${this.fmt(status.target)}<small> °C</small></strong><span class="tile-detail next-target">${next ? `Nächster ${this.nextTime(next.time)} → ${this.fmt(Math.max(5, Math.min(35, next.temperature + (status.offset || 0))))} °C` : "Kein nächster Sollwert"}</span></button></div></div><div class="regulation-preview">${status.state !== "active" && status.message ? `<p>${esc(status.message)}${status.state === "manual" ? ` · bis ${this.nextTime(status.manual_until)}` : ""}</p>` : ""}<div class="preview-line"><span>Grundöffnung ${status.learning_count ? "· gelernt" : "· Startwert"}</span><strong>${this.fmt(status.hold_valve, 0)} %</strong></div><div class="preview-line"><span>Mindestöffnung · Automatik</span><strong>${this.fmt(status.minimum_valve ?? room.minimum_valve ?? 0, 0)} %</strong></div><div class="preview-line"><span>Vorheizen</span><strong>${esc(preheatLabel)}</strong></div><div class="preview-line"><span>Plan + Außenkorrektur</span><strong>${this.fmt(status.base)}° ${status.offset >= 0 ? "+" : "−"} ${this.fmt(Math.abs(status.offset))}°</strong></div><div class="preview-line exercise-line" title="${esc(this.data.valve_exercise?.[room.id]?.last_run ? `Letzte Fahrt ${this.exerciseTime(this.data.valve_exercise[room.id].last_run)}` : "Noch keine abgeschlossene Fahrt")}"><span>Ventilschutz</span><strong>${esc(this.exerciseLabel(room.id))}</strong></div></div>
    </article>`;
  }

  renderEditor() {
    const d = this.draft;
    return `<div class="editor-head"><div class="eyebrow">${d.id ? "RAUM BEARBEITEN" : "NEUER RAUM"}</div><h2>${d.id ? esc(d.name) : "Raum hinzufügen"}</h2><p>Verbinde die Entitäten und gestalte danach den Wochenplan.</p></div>
      <div class="editor-grid"><section class="glass form-card"><div class="card-head"><div><div class="eyebrow">01 · GRUNDLAGEN</div><h3>Raum & Entitäten</h3></div></div>
      <label>Raumname<input data-field="name" maxlength="60" placeholder="z. B. Wohnzimmer" value="${esc(d.name)}"></label>
      <label>Ist-Temperatur${this.entityPicker("actual_entity", "sensor", d.actual_entity)}<small>Temperaturfühler im Raum, in °C</small></label>
      <div class="offset-pair"><label>Bei −15 °C außen<input type="number" min="-2" max="2" step="0.1" data-field="cold_offset" value="${d.cold_offset ?? 1}"><small>Sollwert-Korrektur in °C</small></label><label>Bei +15 °C außen<input type="number" min="-2" max="2" step="0.1" data-field="warm_offset" value="${d.warm_offset ?? -1}"><small>Sollwert-Korrektur in °C</small></label></div>
      <label>Ventil-Ziel (0–100 %)${this.entityPicker("valve_entity", "number", d.valve_entity)}<small>Die Integration schreibt hier die berechnete Ventilstellung.</small></label>
      <label>Ventil-Mindestöffnung<input type="number" min="0" max="100" step="1" data-field="minimum_valve" value="${d.minimum_valve ?? 0}"><small>Prozent · bei Automatik entweder 0 % oder mindestens dieser Wert. 0 % deaktiviert die Mindestöffnung.</small></label>
      <label class="toggle-row"><span><strong>Regelung aktiv</strong><small>Bei Pause bleibt die letzte Ventilstellung bestehen.</small></span><input type="checkbox" data-field="enabled" ${d.enabled ? "checked" : ""}></label>
      </section><section class="glass schedule-card"><div class="card-head"><div><div class="eyebrow">02 · ZEITKANAL</div><h3>Wochenplan</h3></div><span class="pill">1-Minuten-Schritte</span></div>
      <div class="days">${DAYS.map((day, i) => `<button class="day ${this.day === i ? "active" : ""}" data-action="day" data-day="${i}">${SHORT_DAYS[i]}</button>`).join("")}</div>
      ${this.graph(d.schedule, this.day, true)}<p class="graph-hint">Linie zwischen zwei Punkten nach oben oder unten ziehen, um den Sollwert zu ändern. Tippen fügt einen Schaltpunkt hinzu.</p><div class="slot-title"><strong>${DAYS[this.day]}</strong><button class="text-button" data-action="copy-day">Auf alle Tage kopieren</button></div>
      <div class="slot-list">${d.schedule[this.day].map((slot, i) => `<div class="slot"><span class="slot-index">${String(i + 1).padStart(2, "0")}</span><label>Uhrzeit<input type="time" step="60" data-field="slot-time" data-index="${i}" value="${time(slot.minute)}"></label><label>Sollwert<input type="number" min="5" max="35" step="0.1" data-field="slot-temperature" data-index="${i}" value="${slot.temperature}"></label><button class="remove" data-action="remove-slot" data-index="${i}" aria-label="Schaltpunkt entfernen">×</button></div>`).join("") || `<p class="muted">Kein eigener Schaltpunkt. Der letzte Sollwert des Vortags gilt weiter.</p>`}</div>
      <button class="add-slot" data-action="add-slot">＋ Schaltpunkt hinzufügen</button></section></div>
      <div class="editor-actions"><button class="subtle" data-action="cancel">Abbrechen</button>${d.id ? `<button class="danger" data-action="delete">Raum löschen</button>` : ""}<button class="primary" data-action="save" ${this.busy ? "disabled" : ""}>${this.busy ? "Speichert …" : "Raum speichern"}</button></div>`;
  }
}

const CSS = `
:host{display:block;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;color:var(--primary-text-color,#203047)}
*{box-sizing:border-box}button,input,select{font:inherit}button{cursor:pointer}main{min-height:100vh;padding:30px 24px 80px;background:radial-gradient(circle at 14% 6%,rgba(121,176,248,.22),transparent 30%),radial-gradient(circle at 94% 12%,rgba(187,151,247,.19),transparent 32%),linear-gradient(135deg,#edf3fb,#f9fbff 50%,#eaf2fa);color:#24374e}main.dark{background:radial-gradient(circle at 12% 4%,rgba(76,120,187,.22),transparent 32%),radial-gradient(circle at 90% 10%,rgba(113,78,154,.18),transparent 36%),linear-gradient(135deg,#101927,#162338 55%,#142234);color:#edf4ff}
.shell{max-width:1320px;margin:0 auto}.glass{background:rgba(255,255,255,.57);border:1px solid rgba(255,255,255,.82);box-shadow:0 12px 36px rgba(51,83,121,.09),inset 0 1px rgba(255,255,255,.7);backdrop-filter:blur(28px) saturate(145%);-webkit-backdrop-filter:blur(28px) saturate(145%);border-radius:27px}.dark .glass{background:rgba(32,48,70,.53);border-color:rgba(181,209,248,.16);box-shadow:0 16px 42px rgba(0,0,0,.18),inset 0 1px rgba(255,255,255,.07)}header{display:flex;justify-content:space-between;align-items:end;gap:24px;margin:12px 0 34px}h1,h2,h3,p{margin:0}h1{font-size:clamp(2.2rem,4vw,3.7rem);font-weight:680;letter-spacing:-.055em;line-height:1.06;margin:9px 0}h2{font-size:2rem;letter-spacing:-.045em;font-weight:670}h3{font-size:1.35rem;letter-spacing:-.035em;font-weight:650}.eyebrow,.section-label,.metric-label{font-size:.69rem;letter-spacing:.16em;font-weight:750;color:#7090b3}.dark .eyebrow,.dark .section-label,.dark .metric-label{color:#94b8db}.subtitle,.editor-head p{color:#7290aa}.dark .subtitle,.dark .editor-head p{color:#a8bfd7}.primary{border:1px solid rgba(255,255,255,.62);color:white;background:linear-gradient(135deg,#588de1,#7aa6e9);padding:13px 21px;border-radius:16px;font-weight:700;box-shadow:0 7px 19px rgba(74,126,204,.25),inset 0 1px rgba(255,255,255,.35)}.primary:hover{filter:brightness(1.06)}.primary:disabled{opacity:.65;cursor:wait}.plus{font-size:1.25rem;margin-right:5px}.layout{display:grid;grid-template-columns:285px minmax(0,1fr);gap:22px}.rooms{padding:19px;align-self:start}.section-label{padding:9px 9px 17px;display:flex;justify-content:space-between}.section-label span{letter-spacing:0}.room-item{width:100%;display:flex;align-items:center;text-align:left;gap:13px;padding:13px;border:1px solid transparent;border-radius:18px;color:inherit;background:transparent;margin-bottom:5px}.room-item:hover,.room-item.selected{background:rgba(255,255,255,.66);border-color:rgba(255,255,255,.8)}.dark .room-item:hover,.dark .room-item.selected{background:rgba(166,200,246,.12);border-color:rgba(166,200,246,.15)}.room-symbol{display:grid;place-items:center;width:38px;height:38px;border-radius:13px;background:rgba(111,164,233,.19);color:#5e91d7;font-size:1.6rem}.room-copy{display:flex;flex-direction:column;min-width:0;flex:1}.room-copy strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.room-copy small,.metric-foot{font-size:.75rem;color:#7990a7}.dark .room-copy small,.dark .metric-foot{color:#a8bfd7}.room-temp{font-weight:700;color:#567fac}.detail{min-width:0}.title-row,.card-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 2px 18px}.title-row h2{margin-top:5px}.subtle,.back,.text-button{border:0;background:rgba(255,255,255,.52);color:inherit;border-radius:13px;padding:10px 15px;font-weight:650}.dark .subtle,.dark .back,.dark .text-button{background:rgba(255,255,255,.09)}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.metric{padding:23px;min-height:171px;display:flex;flex-direction:column}.metric strong{font-size:2.85rem;line-height:1.1;letter-spacing:-.06em;margin:19px 0 13px}.metric strong small{font-size:1.2rem;letter-spacing:0}.metric-foot{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.metric.accent{background:linear-gradient(145deg,rgba(154,194,255,.34),rgba(255,255,255,.57))}.dark .metric.accent{background:linear-gradient(145deg,rgba(91,145,218,.25),rgba(42,63,89,.56))}.status{display:flex;align-items:center;gap:13px;padding:15px 20px;margin:16px 0}.status-dot{width:10px;height:10px;border-radius:50%;background:#78ba93;box-shadow:0 0 0 5px rgba(120,186,147,.17)}.status.error .status-dot{background:#de8d75;box-shadow:0 0 0 5px rgba(222,141,117,.18)}.status.window .status-dot{background:#e4bb70;box-shadow:0 0 0 5px rgba(228,187,112,.18)}.status strong{font-size:.86rem}.status p{font-size:.77rem;color:#7990a7;margin-top:3px}.dark .status p{color:#a8bfd7}.week,.schedule-card{padding:25px}.pill{background:rgba(113,160,223,.16);color:#5b85b9;padding:7px 11px;border-radius:30px;font-size:.7rem;font-weight:700}.dark .pill{color:#b4d3f6}.days{display:grid;grid-template-columns:repeat(7,1fr);gap:7px;margin:23px 0}.day{border:1px solid rgba(130,161,199,.16);background:rgba(255,255,255,.44);color:inherit;border-radius:13px;padding:9px 5px;font-weight:690}.day.active{color:white;background:#7da9e7;box-shadow:0 5px 13px rgba(96,151,220,.24)}.dark .day{background:rgba(255,255,255,.07)}.dark .day.active{background:#5f90d0}.graph{height:185px;border-radius:16px;overflow:hidden;background:linear-gradient(180deg,rgba(183,214,251,.17),rgba(255,255,255,.12));border:1px solid rgba(137,173,217,.17);padding:15px 13px 8px}.graph svg{width:100%;height:140px;overflow:visible}.graph.empty{display:grid;place-items:center;color:#7990a7;font-size:.85rem}.graph.interactive{cursor:crosshair}.graph-hint{font-size:.71rem;color:#7791ac;margin:7px 1px 0}.axis{display:flex;justify-content:space-between;font-size:.66rem;color:#8aa0b7;margin-top:-3px}.slots-preview{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}.slots-preview div{display:flex;gap:12px;padding:10px 13px;background:rgba(255,255,255,.46);border-radius:12px;font-size:.78rem}.dark .slots-preview div{background:rgba(255,255,255,.08)}.slots-preview span{color:#7791ac}.welcome{text-align:center;max-width:540px;margin:50px auto;padding:55px 40px}.welcome-icon{font-size:4rem;color:#79a5df}.welcome h2{margin:8px 0 10px}.welcome p{line-height:1.6;color:#7290aa;margin-bottom:22px}.editor-head{margin:8px 0 25px}.editor-head .eyebrow{margin:25px 0 7px}.editor-head p{margin-top:6px}.back{padding-left:0;background:transparent}.editor-grid{display:grid;grid-template-columns:minmax(280px,.85fr) minmax(0,1.4fr);gap:20px}.form-card,.schedule-card{padding:25px}.form-card label{display:flex;flex-direction:column;gap:8px;font-size:.84rem;font-weight:680;margin:20px 0}.form-card input,.form-card select,.slot input{width:100%;border:1px solid rgba(132,164,200,.31);border-radius:13px;background:rgba(255,255,255,.62);color:#263b54;padding:12px 13px;outline:none}.form-card input:focus,.form-card select:focus,.slot input:focus{border-color:#77a6e5;box-shadow:0 0 0 3px rgba(119,166,229,.17)}.dark .form-card input,.dark .form-card select,.dark .slot input{background:#243a54;color:#f3f8ff;border-color:#536f92}.form-card label small,.toggle-row small{font-size:.73rem;color:#8097ae;font-weight:400;line-height:1.4}.dark .form-card label small,.dark .toggle-row small{color:#adc0d4}.form-card .toggle-row{display:flex;flex-direction:row;align-items:center;justify-content:space-between;padding-top:17px;border-top:1px solid rgba(134,165,200,.17)}.toggle-row span{display:flex;flex-direction:column;gap:5px}.toggle-row input{width:20px;height:20px;accent-color:#72a1df}.slot-title{display:flex;justify-content:space-between;align-items:center;margin:20px 0 10px}.text-button{font-size:.76rem;color:#5c8dc9;padding:7px 10px}.slot-list{display:flex;flex-direction:column;gap:8px}.slot{display:grid;grid-template-columns:35px minmax(120px,1fr) minmax(120px,1fr) 34px;gap:10px;align-items:end;padding:9px 12px;border-radius:15px;background:rgba(255,255,255,.39)}.dark .slot{background:rgba(255,255,255,.06)}.slot-index{font-size:.73rem;color:#91a8bf;align-self:center}.slot label{font-size:.68rem;color:#7d94ae;font-weight:650}.slot input{margin-top:5px;padding:9px 10px;font-size:.82rem}.remove{border:0;background:rgba(234,125,126,.13);color:#c55f66;border-radius:10px;height:35px;font-size:1.3rem}.add-slot{width:100%;margin-top:12px;border:1px dashed rgba(112,155,210,.51);background:rgba(119,166,229,.08);color:#5b8bc8;padding:12px;border-radius:13px;font-weight:700}.muted{color:#8299af;font-size:.82rem;padding:12px}.editor-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:22px}.danger{margin-right:auto;border:0;background:rgba(220,106,105,.13);color:#bd5b5e;border-radius:13px;padding:10px 15px;font-weight:680}.alert{background:#fff2ee;border:1px solid #e5b7aa;color:#9f4e43;border-radius:14px;padding:12px 16px;margin-bottom:20px}.dark .alert{background:#57383b;color:#ffd2c7}button:focus-visible{outline:3px solid #6c9cdb;outline-offset:2px}
.overview-intro{display:flex;align-items:end;justify-content:space-between;gap:18px;margin:0 0 18px}.overview-intro h2{margin-top:7px}.overview-count{color:#6487ae;font-size:.88rem;font-weight:650}.overview-count span{padding:0 7px;opacity:.5}.dark .overview-count{color:#a9c6e5}.overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.room-card{padding:24px;min-width:0}.room-card-head{display:flex;align-items:start;justify-content:space-between;gap:12px}.room-card-head h3{font-size:1.65rem;margin-top:5px}.room-card-tools{display:flex;align-items:center;gap:9px}.state-chip{display:flex;align-items:center;gap:7px;padding:8px 11px;border-radius:30px;background:rgba(110,186,148,.13);color:#4a8e6b;font-size:.71rem;font-weight:740;white-space:nowrap}.state-chip i{width:7px;height:7px;border-radius:50%;background:currentColor}.state-chip.manual,.state-chip.window{background:rgba(222,180,107,.17);color:#a77d3e}.state-chip.error{background:rgba(218,126,122,.15);color:#bd6660}.dark .state-chip{color:#a5dfbd}.dark .state-chip.manual,.dark .state-chip.window{color:#efd49f}.dark .state-chip.error{color:#f4b4ac}.gear{height:36px;width:36px;border:1px solid rgba(122,160,207,.23);border-radius:12px;background:rgba(255,255,255,.5);color:#7095c3;font-size:1.05rem}.gear:hover{background:rgba(113,160,223,.18)}.dark .gear{background:rgba(255,255,255,.07);color:#b5d5f5}.live-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:20px 0 14px}.live-metrics>div,.valve-tile{display:flex;flex-direction:column;align-items:start;min-width:0;padding:14px 13px;border-radius:17px;border:1px solid rgba(136,172,218,.14);background:rgba(255,255,255,.46);color:inherit;text-align:left}.dark .live-metrics>div,.dark .valve-tile{background:rgba(255,255,255,.07)}.live-metrics span{font-size:.65rem;letter-spacing:.13em;font-weight:780;color:#7d9ab9}.live-metrics strong{font-size:clamp(1.55rem,2.6vw,2.4rem);letter-spacing:-.055em;margin-top:8px;font-weight:680;white-space:nowrap}.live-metrics strong small{font-size:.85rem;letter-spacing:0}.valve-tile{border-color:rgba(103,158,229,.28);background:linear-gradient(145deg,rgba(151,198,255,.27),rgba(255,255,255,.52));cursor:pointer}.valve-tile:hover{border-color:#79a8e8;box-shadow:0 5px 17px rgba(99,151,218,.14)}.valve-tile span{display:flex;width:100%;justify-content:space-between}.valve-tile b{font-size:.9rem;letter-spacing:0}.dark .valve-tile{background:rgba(97,147,215,.19)}.outdoor-strip{display:flex;flex-wrap:wrap;gap:7px 14px;color:#7290ab;font-size:.73rem;padding:4px 2px 16px}.outdoor-strip em{font-style:normal;opacity:.75}.dark .outdoor-strip{color:#aac4dd}.regulation-preview{border-top:1px solid rgba(118,155,200,.19);padding-top:15px}.preview-top{display:flex;align-items:center;justify-content:space-between;gap:10px}.preview-top span,.upcoming>span{color:#7598bc;font-size:.66rem;letter-spacing:.14em;font-weight:780}.preview-top strong{color:#658fc7;font-size:.8rem}.dark .preview-top strong{color:#a9cff5}.regulation-preview p{color:#8096ae;font-size:.74rem;margin:7px 0 14px}.dark .regulation-preview p{color:#aac2d8}.preview-line,.upcoming>div{display:flex;justify-content:space-between;gap:12px;padding:7px 0;font-size:.79rem}.preview-line span,.upcoming time{color:#8298b0}.dark .preview-line span,.dark .upcoming time{color:#abc2d9}.preview-line strong,.upcoming strong{font-weight:700}.upcoming{margin-top:13px;padding-top:12px;border-top:1px solid rgba(118,155,200,.15)}.upcoming>span{display:block;margin-bottom:6px}.regulation-preview>small{display:block;color:#91a5ba;font-size:.68rem;margin-top:13px}.dark .regulation-preview>small{color:#94acc4}.offset-pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}.form-card .offset-pair label{margin:7px 0 12px}.graph [data-segment]{cursor:ns-resize;touch-action:none}.graph.interactive svg{touch-action:none}
.entity-picker{position:relative}.entity-results{position:absolute;z-index:20;top:calc(100% + 5px);left:0;right:0;max-height:260px;overflow:auto;padding:5px;border:1px solid rgba(132,164,200,.32);border-radius:14px;background:#f7fbff;box-shadow:0 16px 35px rgba(42,69,103,.2)}.entity-results[hidden]{display:none}.entity-results button{display:flex;flex-direction:column;gap:3px;width:100%;padding:9px 11px;border:0;border-radius:9px;background:transparent;color:#263b54;text-align:left}.entity-results button:hover,.entity-results button:focus-visible{background:#e3efff;outline:none}.entity-results button strong{font-size:.82rem;font-weight:650}.entity-results button small,.entity-empty{font-size:.72rem;color:#7189a4}.entity-empty{padding:10px}.dark .entity-results{background:#22364f;border-color:#536f92}.dark .entity-results button{color:#f3f8ff}.dark .entity-results button:hover,.dark .entity-results button:focus-visible{background:#345477}.dark .entity-results button small,.dark .entity-empty{color:#abc2d9}
.manual-stop{border:0;cursor:pointer}.manual-stop:hover{filter:brightness(1.08);box-shadow:0 0 0 2px rgba(167,125,62,.25)}
.room-card-error{margin:13px 0 0;padding:9px 12px;border-radius:12px;background:rgba(218,126,122,.14);color:#a95752;font-size:.75rem;font-weight:620}.dark .room-card-error{color:#f4b4ac}
.weather-overview{display:flex;flex-wrap:wrap;gap:10px 24px;align-items:center;margin-bottom:18px;padding:12px 20px;border-radius:18px}.weather-values{display:flex;flex-wrap:wrap;align-items:center;gap:6px 17px;font-size:.78rem;color:#7290ab}.weather-values strong{color:inherit;font-size:.72rem}.weather-values b{color:#4d739e;font-weight:700}.weather-values em{font-style:normal;opacity:.75}.dark .weather-values{color:#aac4dd}.dark .weather-values b{color:#e1efff}
.header-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.central-settings{max-width:560px}.central-settings label{margin:0}.central-settings+.editor-actions{max-width:560px}
.header-actions .header-icon{display:grid;place-items:center;width:48px;height:48px;padding:0;font-size:1.4rem;line-height:1}
.room-card-tools{flex-wrap:wrap;justify-content:flex-end}.room-card-head>div:first-child{min-width:0}
.room-order-handle{height:36px;width:36px;border:1px solid rgba(122,160,207,.23);border-radius:12px;background:rgba(255,255,255,.5);color:#7095c3;font-size:1.3rem;line-height:1;cursor:grab;touch-action:none;user-select:none}.room-order-handle:active{cursor:grabbing}.dark .room-order-handle{background:rgba(255,255,255,.07);color:#b5d5f5}.room-dragging{opacity:.66;border-color:#79a8e8;box-shadow:0 15px 42px rgba(79,132,202,.23)}
.window-status{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px;padding:10px 13px;border:1px solid rgba(136,172,218,.15);border-radius:14px;background:rgba(255,255,255,.27)}.dark .window-status{background:rgba(255,255,255,.04)}.window-status>span{font-size:.66rem;letter-spacing:.13em;font-weight:780;color:#7d9ab9}.window-status strong{display:flex;align-items:center;gap:7px;font-size:.76rem;font-weight:700}.window-status i{width:7px;height:7px;border-radius:50%;background:currentColor}.window-status.ok strong{color:#4a8e6b}.dark .window-status.ok strong{color:#a5dfbd}.window-status.alarm{border-color:rgba(222,180,107,.35);background:rgba(222,180,107,.12)}.window-status.alarm strong{color:#a77d3e}.dark .window-status.alarm strong{color:#efd49f}.window-status.unknown strong{color:#8b99aa}.dark .window-status.unknown strong{color:#b5c1d1}
.shell{max-width:1600px}.overview-grid{grid-template-columns:1fr}.room-card{padding:25px 28px}.room-card-body{display:grid;grid-template-columns:minmax(0,1.12fr) minmax(0,.88fr);gap:34px;margin-top:19px}.room-summary,.regulation-preview{min-width:0}.live-metrics{margin:0;gap:11px}.temperature-tile{display:flex;flex-direction:column;align-items:start;min-width:0;padding:14px 13px;border-radius:17px;border:1px solid rgba(136,172,218,.14);background:rgba(255,255,255,.46);color:inherit;text-align:left;cursor:pointer}.dark .temperature-tile{background:rgba(255,255,255,.07)}.temperature-tile:hover{border-color:#79a8e8;box-shadow:0 5px 17px rgba(99,151,218,.14)}.temperature-tile span{display:flex;justify-content:space-between;width:100%;font-size:.65rem;letter-spacing:.13em;font-weight:780;color:#7d9ab9}.temperature-tile span b{font-size:.9rem;letter-spacing:0}.temperature-tile strong{font-size:clamp(1.55rem,2.4vw,2.3rem);letter-spacing:-.055em;margin-top:8px;font-weight:680;white-space:nowrap}.temperature-tile strong small{font-size:.85rem;letter-spacing:0}.regulation-preview{border-top:0;border-left:1px solid rgba(118,155,200,.19);padding:0 0 0 30px}.upcoming{margin-top:18px}.regulation-preview>small{margin-top:19px}
@media(max-width:900px){.room-card-body{grid-template-columns:1fr;gap:18px}.regulation-preview{border-left:0;border-top:1px solid rgba(118,155,200,.19);padding:17px 0 0}.live-metrics{margin:0}}
@media(max-width:640px){.header-actions{width:100%;display:grid;grid-template-columns:1fr}.header-actions button{width:100%}.live-metrics{gap:6px}.temperature-tile{padding:12px 8px}.temperature-tile strong{font-size:1.62rem}}
@media(max-width:950px){.layout,.editor-grid{grid-template-columns:1fr}.rooms{display:flex;gap:8px;overflow:auto;align-items:center}.section-label{flex:none;display:none}.room-item{width:190px;flex:none;margin:0}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.overview-grid{grid-template-columns:1fr}}

/* Room overview: compact rows with the key values and regulation always visible. */
main.light{background:radial-gradient(ellipse 48% 35% at 25% 35%,rgba(202,220,238,.48),transparent 80%),linear-gradient(115deg,rgba(224,232,239,.82),rgba(237,231,216,.78)),url("/room_thermostat/frontend/room-background.png") center center / cover fixed,#dfe5e9}
main.dark{background:radial-gradient(ellipse 44% 32% at 36% 34%,rgba(170,193,216,.16),transparent 78%),radial-gradient(ellipse 38% 42% at 78% 72%,rgba(186,181,158,.11),transparent 80%),linear-gradient(rgba(13,17,24,.26),rgba(13,17,24,.26)),url("/room_thermostat/frontend/room-background.png") center center / cover fixed,#242422}
.light .room-card.glass{background:linear-gradient(155deg,rgba(249,251,253,.78),rgba(239,245,249,.68));border-color:rgba(255,255,255,.8);box-shadow:0 15px 38px rgba(42,55,71,.15),0 2px 6px rgba(42,55,71,.07),inset 0 1px rgba(255,255,255,.9),inset 0 -1px rgba(70,86,104,.13)}
.dark .room-card.glass{background:linear-gradient(155deg,rgba(35,39,43,.77),rgba(35,39,43,.69));border-color:rgba(235,209,160,.27);box-shadow:0 18px 46px rgba(0,0,0,.42),0 2px 9px rgba(0,0,0,.22),inset 0 1px rgba(255,255,255,.2),inset 0 -1px rgba(0,0,0,.24)}
.dark .room-card .temperature-tile{background:rgba(255,255,255,.045)}
.dark .room-card .valve-tile:not(.heating){background:rgba(97,147,215,.13)}
.overview-grid{grid-template-columns:1fr;gap:11px}
.room-card{padding:13px 18px}
.room-card-head{align-items:center}
.editor-head .eyebrow{margin-top:0}
.room-card-head h3{font-size:1.3rem;margin:0}
.room-card-body{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:24px;margin-top:8px}
.live-metrics{gap:8px}
.temperature-tile,.valve-tile{padding:8px 11px;border-radius:12px}
.temperature-tile strong,.live-metrics strong{font-size:clamp(1.4rem,2vw,1.75rem);margin-top:2px}
.live-metrics .tile-detail{display:block;width:100%;margin-top:auto;padding-top:4px;color:#7892b0;font-size:.68rem;font-weight:580;letter-spacing:0;text-align:right;line-height:1.2}
.dark .live-metrics .tile-detail{color:#a7bbd2}
.valve-tile span{align-items:center;gap:5px}
.valve-tile .valve-trend{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:0;font-size:.7rem;font-style:normal;font-weight:600;text-transform:none}
.valve-tile .valve-trend.opening{color:#b96758}
.valve-tile .valve-trend.closing{color:#4f9571}
.dark .valve-tile .valve-trend.opening{color:#f1ad98}
.dark .valve-tile .valve-trend.closing{color:#9cdbb4}
.valve-tile span b{margin-left:auto}
.room-card-tools .window-status{display:inline-flex;align-items:center;justify-content:center;gap:7px;margin:0;padding:7px 10px;border-radius:30px;background:rgba(110,186,148,.13);border:0;color:#4a8e6b;font-size:.71rem;font-weight:740;white-space:nowrap}
.room-card-tools .window-status i{flex:none;width:7px;height:7px;background:currentColor}
.dark .room-card-tools .window-status.ok{color:#a5dfbd}
.room-card-tools .window-status.alarm{background:rgba(222,180,107,.17);color:#a77d3e}
.dark .room-card-tools .window-status.alarm{color:#efd49f}
.room-card-tools .window-status.unknown{background:rgba(139,153,170,.15);color:#8b99aa}
.dark .room-card-tools .window-status.unknown{color:#b5c1d1}
.state-chip.paused{background:rgba(200,105,101,.14);color:#aa5d5b}
.dark .state-chip.paused{background:rgba(215,112,106,.17);color:#efaaa5}
.state-chip.blocked{background:rgba(193,147,70,.18);color:#926517}
.dark .state-chip.blocked{background:rgba(230,182,98,.17);color:#f3d29b}
.state-chip.exercise{background:rgba(112,157,220,.18);color:#4e7db8}
.dark .state-chip.exercise{color:#bbd9ff}
.valve-tile.heating{border-color:rgba(198,134,91,.42);background:linear-gradient(145deg,rgba(231,176,105,.22),rgba(208,108,88,.14))}
.dark .valve-tile.heating{border-color:rgba(236,177,117,.4);background:linear-gradient(145deg,rgba(211,144,81,.26),rgba(185,92,74,.18))}
.valve-tile.heating:hover{border-color:rgba(204,123,83,.72);box-shadow:0 5px 17px rgba(173,98,70,.15)}
.weather-overview{justify-content:space-between}.weather-gate{display:flex;align-items:center;gap:9px;font-size:.75rem;color:#7290ab}.weather-gate strong{padding:6px 10px;border-radius:20px;background:rgba(84,166,115,.16);color:#397e55;font-weight:700}.weather-gate.blocked strong{background:rgba(193,147,70,.18);color:#926517}.weather-gate.unavailable strong,.weather-gate.disabled strong{background:rgba(125,148,170,.17);color:#607a93}.dark .weather-gate{color:#aac4dd}.dark .weather-gate strong{color:#a5dfbd}.dark .weather-gate.blocked strong{color:#f3d29b}.dark .weather-gate.unavailable strong,.dark .weather-gate.disabled strong{color:#becfe1}
.central-settings .gate-settings{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.central-settings .gate-settings label{min-width:0;margin:0}.central-settings .toggle-row{margin:22px 0;padding-top:20px}.central-settings .gate-settings input{min-width:0}
.regulation-preview{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:16px;align-content:center;border-top:0;border-left:1px solid rgba(118,155,200,.19);padding:0 0 0 22px;margin:0}
.regulation-preview p{grid-column:1/-1;margin:0 0 4px}
.preview-line{min-width:0;align-items:baseline;padding:3px 0}
.preview-line span{min-width:0}
.preview-line strong{flex:none;white-space:nowrap}
.exercise-line{grid-column:1/-1;border-top:1px solid rgba(118,155,200,.15);margin-top:5px;padding-top:8px}.exercise-line strong{white-space:normal;text-align:right}
@media(max-width:1150px){.room-card-body{grid-template-columns:1fr;gap:10px}.regulation-preview{border-left:0;border-top:1px solid rgba(118,155,200,.19);padding:8px 0 0}}
@media(max-width:640px){.temperature-tile,.valve-tile{padding:10px 8px}.regulation-preview{grid-template-columns:1fr}.valve-tile span{flex-wrap:wrap}.valve-tile .valve-trend{order:3;flex-basis:100%;font-size:.6rem}.live-metrics .tile-detail{font-size:.61rem;overflow-wrap:anywhere}}
@media(max-width:640px){.weather-gate{width:100%;justify-content:space-between}.central-settings .gate-settings{grid-template-columns:1fr}}
@media(max-width:640px){.header-actions{display:flex;width:auto;align-self:flex-end}.header-actions .header-icon{width:48px}}
@media(max-width:640px){main{padding:18px 13px 70px}header{align-items:start;flex-direction:column;gap:15px;margin-bottom:24px}header .primary{width:100%}.glass{border-radius:22px}h2{font-size:1.65rem}.title-row{align-items:end}.metrics{grid-template-columns:1fr 1fr;gap:9px}.metric{padding:17px;min-height:135px}.metric strong{font-size:2.25rem;margin:14px 0 8px}.metric:last-child{grid-column:span 2}.metric-foot{font-size:.69rem}.week,.form-card,.schedule-card{padding:17px}.day{font-size:.7rem}.slot{grid-template-columns:24px 1fr 1fr 30px;gap:5px;padding:7px}.slot input{padding:8px 4px}.editor-actions{flex-wrap:wrap}.editor-actions .primary{flex:1}.overview-intro{align-items:start}.overview-count{font-size:.75rem}.room-card{padding:17px}.room-card-head h3{font-size:1.42rem}.live-metrics{gap:6px}.live-metrics>div,.valve-tile{padding:12px 8px}.live-metrics strong{font-size:1.62rem}.state-chip{padding:7px;font-size:.65rem}.outdoor-strip{font-size:.7rem}.offset-pair{gap:7px}}
/* Keep the light theme translucent while giving small dashboard labels clear contrast. */
main.light{color:#26374b}
.light .eyebrow,.light .subtitle,.light .weather-values,.light .weather-gate{color:#52667d}
.light .weather-values b{color:#344e69}
.light .weather-overview.glass{background:linear-gradient(120deg,rgba(241,247,251,.78),rgba(249,247,241,.68));border-color:rgba(255,255,255,.8);box-shadow:0 10px 28px rgba(42,55,71,.11),inset 0 1px rgba(255,255,255,.86)}
.light .room-card .temperature-tile{background:rgba(230,239,247,.55);border-color:rgba(100,129,159,.25)}
.light .room-card .valve-tile:not(.heating){background:linear-gradient(145deg,rgba(207,225,248,.65),rgba(231,241,250,.5));border-color:rgba(94,139,197,.36)}
.light .room-card .valve-tile.heating{background:linear-gradient(145deg,rgba(242,218,185,.6),rgba(245,230,211,.46));border-color:rgba(169,116,80,.38)}
.light .room-card .temperature-tile:hover,.light .room-card .valve-tile:hover{border-color:#557fb2;box-shadow:0 5px 16px rgba(50,83,119,.12)}
.light .live-metrics span,.light .temperature-tile span,.light .live-metrics .tile-detail,.light .preview-line span,.light .regulation-preview p{color:#53677d}
.light .room-card-tools .window-status.ok,.light .room-card-tools .state-chip.active{color:#315e46;background:rgba(91,151,112,.15)}
.light .room-card-tools .gear,.light .room-card-tools .room-order-handle{color:#496683;border-color:rgba(99,128,158,.32);background:rgba(248,251,253,.58)}
.light .header-actions .primary{background:linear-gradient(135deg,#426cb4,#587fc5)}
.light .valve-tile .valve-trend.opening{color:#874d39}
@media(max-width:640px){.light .live-metrics .tile-detail{font-size:.67rem;line-height:1.25}}
/* Smoky glass palette inspired by the dashboard reference, shared by overview and editor. */
:host{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main.dark{color:#f4f2ed;background:radial-gradient(ellipse 33% 37% at 24% 39%,rgba(69,113,118,.42),transparent 76%),radial-gradient(ellipse 36% 35% at 78% 42%,rgba(169,116,70,.4),transparent 77%),radial-gradient(ellipse 33% 39% at 56% 76%,rgba(111,124,76,.25),transparent 82%),linear-gradient(rgba(10,12,12,.06),rgba(10,12,12,.14)),url("/room_thermostat/frontend/room-background-smoky.png") center center / cover fixed,#171917}
.dark .glass{background:linear-gradient(145deg,rgba(55,56,53,.67),rgba(39,42,42,.7));border-color:rgba(255,255,255,.22);box-shadow:0 18px 44px rgba(0,0,0,.3),inset 0 1px rgba(255,255,255,.12);backdrop-filter:blur(32px) saturate(115%);-webkit-backdrop-filter:blur(32px) saturate(115%)}
.dark .room-card.glass{background:linear-gradient(130deg,rgba(54,55,53,.57),rgba(39,42,43,.56) 57%,rgba(62,57,47,.52));border-color:rgba(255,255,255,.32);box-shadow:0 18px 44px rgba(0,0,0,.28),inset 0 1px rgba(255,255,255,.18),inset 0 -1px rgba(0,0,0,.2)}
.dark .weather-overview.glass{background:linear-gradient(110deg,rgba(60,62,60,.58),rgba(57,56,46,.53));border-color:rgba(255,255,255,.26)}
.dark h1{font-size:clamp(2.15rem,3.55vw,3.45rem);font-weight:690;letter-spacing:-.045em}
.dark .eyebrow{color:#d2caaa;letter-spacing:.12em}
.dark .subtitle,.dark .editor-head p{color:#d2d4d0}
.dark .weather-values,.dark .weather-gate{color:#dde0da;font-size:.78rem}
.dark .weather-values b{color:#fff;font-size:.78rem}
.dark .weather-values em{opacity:1;color:#c8ccc7}
.dark .weather-gate strong{background:rgba(166,190,128,.2);color:#dcf1c6}
.dark .room-card-head h3{color:#fff;font-weight:680}
.dark .room-card .temperature-tile,.dark .room-card .valve-tile:not(.heating){background:linear-gradient(150deg,rgba(255,255,255,.1),rgba(255,255,255,.045));border-color:rgba(255,255,255,.2);box-shadow:inset 0 1px rgba(255,255,255,.11)}
.dark .room-card .valve-tile.heating{background:linear-gradient(145deg,rgba(199,137,69,.3),rgba(109,79,63,.18));border-color:rgba(239,188,119,.44)}
.dark .room-card .temperature-tile:hover,.dark .room-card .valve-tile:hover{border-color:rgba(245,218,166,.7);box-shadow:0 6px 18px rgba(0,0,0,.2)}
.dark .live-metrics span,.dark .temperature-tile span{color:#d5d7d4;letter-spacing:.08em;font-size:.7rem}
.dark .live-metrics strong,.dark .temperature-tile strong{color:#fff;font-weight:720}
.dark .live-metrics .tile-detail{color:#d3d8d7;font-size:.72rem;font-weight:550}
.dark .preview-line{font-size:.82rem}
.dark .preview-line span,.dark .regulation-preview p{color:#d0d4d1}
.dark .preview-line strong{color:#f8f8f4}
.dark .regulation-preview,.dark .exercise-line{border-color:rgba(255,255,255,.18)}
.dark .room-card-tools .state-chip.active,.dark .room-card-tools .window-status.ok{background:rgba(151,184,144,.17);color:#daf0d7}
.dark .room-card-tools .gear,.dark .room-card-tools .room-order-handle,.dark .header-actions .subtle{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.24);color:#f2f3ef;box-shadow:inset 0 1px rgba(255,255,255,.08)}
.dark .header-actions .primary,.dark .editor-actions .primary,.dark .welcome .primary{background:linear-gradient(140deg,#a86738,#c78243);border-color:rgba(255,220,166,.45);box-shadow:0 8px 20px rgba(80,45,25,.24),inset 0 1px rgba(255,255,255,.23)}
.dark .form-card input,.dark .form-card select,.dark .slot input{background:rgba(21,23,23,.55);border-color:rgba(255,255,255,.28);color:#fff}
.dark .form-card label small,.dark .toggle-row small,.dark .graph-hint,.dark .axis,.dark .slot-index,.dark .slot label{color:#cbd0ce}
.dark .day,.dark .slot{background:rgba(255,255,255,.075);border-color:rgba(255,255,255,.17);color:#f5f5f1}
.dark .day.active{background:#9d673e;border-color:#d19b64;color:#fff}
.dark .pill,.dark .add-slot{background:rgba(200,143,79,.16);color:#f5d4a6;border-color:rgba(220,169,109,.35)}
.dark .graph{background:linear-gradient(180deg,rgba(209,165,103,.12),rgba(255,255,255,.035));border-color:rgba(255,255,255,.18)}
.dark .entity-results{background:#303331;border-color:rgba(255,255,255,.28)}
.dark .entity-results button:hover,.dark .entity-results button:focus-visible{background:rgba(255,255,255,.12)}
.dark .graph stop{stop-color:#d79d64}
.dark .graph [data-graph-line],.dark .graph [data-graph-point]{stroke:#e2ac72}
.dark .graph [data-graph-point]{fill:#323332}
.dark .toggle-row input{accent-color:#bd824b}
.dark .text-button{color:#f3d4ae;background:rgba(221,166,104,.12)}
.light .header-actions .primary,.light .editor-actions .primary,.light .welcome .primary{background:linear-gradient(140deg,#9b6035,#b6763d);border-color:rgba(116,73,42,.32)}
.light .room-card.glass{background:linear-gradient(135deg,rgba(247,248,247,.77),rgba(235,239,238,.69) 58%,rgba(245,240,230,.64));border-color:rgba(255,255,255,.82)}
.light .room-card .temperature-tile{background:rgba(223,231,230,.45)}
.light .room-card .valve-tile:not(.heating){background:linear-gradient(145deg,rgba(208,223,224,.64),rgba(234,239,233,.46));border-color:rgba(99,132,131,.37)}
.light .eyebrow{color:#686b50}
.light .live-metrics span,.light .temperature-tile span,.light .live-metrics .tile-detail,.light .preview-line span{color:#475b5b}
.light .graph stop{stop-color:#ad8057}
.light .graph [data-graph-line],.light .graph [data-graph-point]{stroke:#a46b3f}
.light .toggle-row input{accent-color:#a56e40}
@media(max-width:640px){.live-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.live-metrics .valve-tile{grid-column:1/-1}.dark .live-metrics .tile-detail,.light .live-metrics .tile-detail{font-size:.7rem;line-height:1.3}.valve-tile .valve-trend{order:initial;flex-basis:auto}}
/* Lighter type hierarchy: let size and contrast carry the important values. */
:host{font-family:"Segoe UI Variable Text","Segoe UI",-apple-system,BlinkMacSystemFont,sans-serif}
h1,.dark h1{font-weight:540;letter-spacing:-.04em}
h2,h3,.dark .room-card-head h3{font-weight:520}
.eyebrow,.dark .eyebrow{font-weight:600}
.room-card-head h3{font-size:1.22rem}
.live-metrics strong,.temperature-tile strong,.dark .live-metrics strong,.dark .temperature-tile strong{font-weight:510;letter-spacing:-.03em}
.live-metrics span,.temperature-tile span,.dark .live-metrics span,.dark .temperature-tile span{font-weight:500;letter-spacing:.065em}
.live-metrics span b,.temperature-tile span b{font-weight:500}
.live-metrics .tile-detail,.dark .live-metrics .tile-detail{font-weight:400;letter-spacing:0}
.valve-tile .valve-trend{font-weight:400}
.preview-line span,.dark .preview-line span{font-weight:400}
.preview-line strong,.dark .preview-line strong{font-weight:500}
.weather-values b,.dark .weather-values b,.weather-gate strong,.dark .weather-gate strong{font-weight:500}
.state-chip,.room-card-tools .window-status{font-weight:500}
.primary,.subtle,.form-card label,.slot label{font-weight:500}
@media(max-width:640px){.room-card-head h3{font-size:1.32rem}}
/* Horizontal navigation and one framed dashboard, adapted from the reference layout. */
main.dark{background:linear-gradient(90deg,rgba(7,8,8,.24),rgba(9,9,8,.42) 48%,rgba(8,7,6,.2)),url("/room_thermostat/frontend/room-interior.png") center center / cover fixed,#171512}
.shell{max-width:1380px}
.top-menu{display:flex;align-items:center;justify-content:space-between;gap:24px;min-height:60px;padding:7px 10px 7px 18px;border-radius:20px;overflow:hidden}
.dark .top-menu.glass{background:rgba(43,42,40,.74);border-color:rgba(255,255,255,.28);box-shadow:0 16px 34px rgba(0,0,0,.25),inset 0 1px rgba(255,255,255,.15)}
.top-menu-brand{display:flex;align-items:center;gap:10px;flex:none;font-size:.92rem;font-weight:560;letter-spacing:-.02em}
.top-menu-mark{display:grid;place-items:center;width:34px;height:34px;border:1px solid rgba(255,255,255,.32);border-radius:11px;font-size:1.2rem;line-height:1}
.top-menu-links{display:flex;align-items:center;justify-content:flex-end;gap:5px;min-width:0;overflow-x:auto;scrollbar-width:none}
.top-menu-links::-webkit-scrollbar{display:none}
.top-menu-link{flex:none;border:1px solid transparent;border-radius:13px;background:transparent;color:inherit;padding:10px 15px;font-size:.83rem;font-weight:500;white-space:nowrap}
.top-menu-link:hover{background:rgba(255,255,255,.1)}
.top-menu-link.active{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.22)}
.top-menu-link.add-room-link{margin-left:5px;background:linear-gradient(135deg,#a96d3d,#bf8551);border-color:rgba(255,217,169,.4);color:#fff}
.dashboard-surface{margin-top:18px;padding:28px;border-radius:24px}
.dark .dashboard-surface.glass{background:linear-gradient(135deg,rgba(55,54,51,.72),rgba(44,43,41,.68) 55%,rgba(84,68,49,.58));border-color:rgba(255,255,255,.27);box-shadow:0 24px 70px rgba(0,0,0,.38),inset 0 1px rgba(255,255,255,.17);backdrop-filter:blur(30px) saturate(115%);-webkit-backdrop-filter:blur(30px) saturate(115%)}
.dashboard-header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 0 25px}
.dashboard-header h1{font-size:clamp(1.85rem,3vw,2.65rem);margin:5px 0 6px}
.dashboard-header .eyebrow{font-size:.65rem}
.dashboard-header-badge{flex:none;border:1px solid rgba(255,255,255,.22);border-radius:14px;padding:10px 13px;background:rgba(255,255,255,.08);font-size:.76rem;color:#eee9e2}
.weather-overview{margin:0 0 22px;padding:16px 18px}
.dark .weather-overview.glass{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.21)}
.weather-values{gap:9px 20px}
.rooms-section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:0 0 13px}
.rooms-section-head h2{font-size:1.12rem;font-weight:550;letter-spacing:-.02em}
.rooms-section-head span{font-size:.73rem;color:#d1ccc4}
.overview-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.room-card{padding:17px 18px;border-radius:20px}
.dark .room-card.glass{background:linear-gradient(145deg,rgba(255,255,255,.09),rgba(31,32,32,.19));border-color:rgba(255,255,255,.2);box-shadow:0 9px 24px rgba(0,0,0,.13),inset 0 1px rgba(255,255,255,.09)}
.room-card-head h3{font-size:1.22rem}
.room-card-tools{gap:6px}
.room-card-body{display:grid;grid-template-columns:1fr;gap:12px;margin-top:12px}
.live-metrics{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.temperature-tile,.valve-tile{min-height:83px}
.dark .room-card .temperature-tile,.dark .room-card .valve-tile:not(.heating){background:rgba(255,255,255,.085);border-color:rgba(255,255,255,.17)}
.regulation-preview{border-left:0;border-top:1px solid rgba(255,255,255,.16);padding:10px 0 0;grid-template-columns:repeat(2,minmax(0,1fr))}
.preview-line{font-size:.75rem}
.light .top-menu.glass{background:rgba(250,251,251,.82);border-color:rgba(255,255,255,.9)}
.light .top-menu-mark{border-color:rgba(68,84,94,.25)}
.light .top-menu-link.active{background:rgba(80,109,119,.13);border-color:rgba(80,109,119,.2)}
.light .dashboard-surface.glass{background:rgba(248,249,248,.7);border-color:rgba(255,255,255,.85);box-shadow:0 20px 54px rgba(46,59,66,.16)}
.light .dashboard-header-badge{color:#31434d;background:rgba(90,120,124,.1);border-color:rgba(80,110,116,.18)}
.light .rooms-section-head span{color:#52666c}
.light .room-card.glass{background:rgba(248,250,249,.62);border-color:rgba(255,255,255,.85)}
.light .regulation-preview{border-top-color:rgba(80,110,116,.18)}
@media(max-width:1100px){.overview-grid{grid-template-columns:1fr}.room-card-body{grid-template-columns:1fr}}
@media(max-width:640px){main{padding:13px 11px 50px}.top-menu{gap:0;padding:6px;border-radius:16px}.top-menu-brand{display:none}.top-menu-links{width:100%;justify-content:flex-start}.top-menu-link{padding:10px 12px;font-size:.76rem}.dashboard-surface{padding:17px 13px;margin-top:12px;border-radius:20px}.dashboard-header{margin-bottom:17px}.dashboard-header h1{font-size:1.82rem}.dashboard-header-badge{display:none}.rooms-section-head span{display:none}.room-card{padding:15px}.live-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.live-metrics .valve-tile{grid-column:1/-1}.regulation-preview{grid-template-columns:1fr}.weather-overview{padding:13px}}
/* Wider, brighter glass with a single horizontal toolbar. */
.shell{max-width:1600px}
.top-menu{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;min-height:58px;padding:7px 9px 7px 16px;border-radius:29px}
.dark .top-menu.glass{background:rgba(59,60,60,.52);border-color:rgba(255,255,255,.35);box-shadow:0 18px 40px rgba(0,0,0,.25),inset 0 1px rgba(255,255,255,.23);backdrop-filter:blur(30px) saturate(130%);-webkit-backdrop-filter:blur(30px) saturate(130%)}
.top-menu-mark{border-radius:12px;background:rgba(255,255,255,.09);font-size:1.05rem}
.top-menu-links{justify-content:center;gap:4px;padding:4px;border:1px solid rgba(255,255,255,.13);border-radius:19px;background:rgba(255,255,255,.075)}
.top-menu-link{display:flex;align-items:center;gap:7px;padding:9px 14px;border-radius:14px}
.top-menu-link span{font-size:1rem;line-height:1}
.top-menu-link.active{background:rgba(255,255,255,.19);border-color:rgba(255,255,255,.26);box-shadow:inset 0 1px rgba(255,255,255,.16)}
.top-menu-add{display:flex;align-items:center;justify-content:center;gap:7px;justify-self:end;min-height:39px;padding:8px 15px;border:1px solid rgba(255,220,176,.52);border-radius:16px;background:linear-gradient(135deg,rgba(170,108,55,.86),rgba(198,138,77,.88));color:#fff;font-size:.82rem;font-weight:530;white-space:nowrap;box-shadow:inset 0 1px rgba(255,255,255,.2),0 6px 16px rgba(61,37,22,.2)}
.top-menu-add:hover{filter:brightness(1.08)}
.top-menu-add>span:first-child{font-size:1.15rem;line-height:1}
.dashboard-surface{padding:30px 34px}
.dark .dashboard-surface.glass{background:radial-gradient(ellipse 42% 65% at 75% 75%,rgba(202,149,83,.19),transparent 78%),radial-gradient(ellipse 37% 52% at 19% 24%,rgba(202,214,212,.1),transparent 75%),linear-gradient(130deg,rgba(84,86,85,.51),rgba(48,49,49,.52) 54%,rgba(102,82,61,.44));border-color:rgba(255,255,255,.36);box-shadow:0 25px 70px rgba(0,0,0,.34),inset 0 1px rgba(255,255,255,.29),inset 0 -1px rgba(0,0,0,.17);backdrop-filter:blur(26px) saturate(128%);-webkit-backdrop-filter:blur(26px) saturate(128%)}
.dark .weather-overview.glass{background:linear-gradient(110deg,rgba(255,255,255,.18),rgba(255,255,255,.09));border-color:rgba(255,255,255,.24);box-shadow:inset 0 1px rgba(255,255,255,.2),0 8px 20px rgba(0,0,0,.12)}
.dark .room-card.glass{background:linear-gradient(145deg,rgba(255,255,255,.13),rgba(255,255,255,.055));border-color:rgba(255,255,255,.25);box-shadow:0 12px 26px rgba(0,0,0,.17),inset 0 1px rgba(255,255,255,.18);backdrop-filter:blur(20px) saturate(120%);-webkit-backdrop-filter:blur(20px) saturate(120%)}
.dark .room-card .temperature-tile,.dark .room-card .valve-tile:not(.heating){background:linear-gradient(150deg,rgba(255,255,255,.16),rgba(255,255,255,.065));border-color:rgba(255,255,255,.22);box-shadow:inset 0 1px rgba(255,255,255,.14)}
.dark .room-card .valve-tile.heating{background:linear-gradient(145deg,rgba(210,152,88,.26),rgba(255,255,255,.075));border-color:rgba(240,190,132,.43)}
.light .top-menu-links{border-color:rgba(82,102,108,.15);background:rgba(98,122,126,.07)}
.light .top-menu-link.active{background:rgba(93,116,121,.17);border-color:rgba(93,116,121,.23)}
.light .dashboard-surface.glass{background:linear-gradient(130deg,rgba(249,251,250,.76),rgba(237,242,240,.65));border-color:rgba(255,255,255,.91);box-shadow:0 20px 54px rgba(46,59,66,.16),inset 0 1px rgba(255,255,255,.95)}
@media(max-width:640px){.top-menu{display:flex;gap:5px;padding:6px;border-radius:19px}.top-menu-links{flex:1;justify-content:flex-start;min-width:0;padding:2px;gap:0;overflow-x:auto}.top-menu-link{padding:8px 10px;font-size:.72rem}.top-menu-link span{font-size:.9rem}.top-menu-add{flex:none;min-width:41px;min-height:38px;padding:7px;border-radius:13px}.top-menu-add .add-label{display:none}.dashboard-surface{padding:17px 13px}}
/* Compact horizontal icon rail on the right; weather belongs beside the title. */
.top-menu{display:flex;align-items:center;justify-content:flex-start;gap:5px;width:max-content;max-width:100%;min-height:0;margin-left:auto;padding:6px;border-radius:22px}
.top-menu-link,.top-menu-add{display:grid;place-items:center;flex:none;min-width:43px;width:43px;height:43px;min-height:43px;padding:0;border-radius:16px}
.top-menu-link span,.top-menu-add>span:first-child{font-size:1.35rem;line-height:1}
.top-menu-add{margin-left:3px}
.dashboard-header{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,auto);align-items:center;gap:28px;margin-bottom:24px}
.dashboard-title{min-width:0}
.dashboard-header .weather-overview{justify-self:end;width:auto;max-width:880px;margin:0;padding:16px 18px;gap:10px 17px}
.dashboard-header .weather-values{gap:7px 17px}
.dashboard-header .weather-gate{white-space:nowrap}
@media(max-width:1200px){.dashboard-header{grid-template-columns:1fr;gap:18px}.dashboard-header .weather-overview{justify-self:stretch;max-width:none}}
@media(max-width:640px){.top-menu{gap:3px;padding:5px;border-radius:18px}.top-menu-link,.top-menu-add{min-width:39px;width:39px;height:39px;min-height:39px;border-radius:13px}.dashboard-header{gap:15px}.dashboard-header .weather-overview{padding:13px}}
/* One room per row and the restrained gold icon rail from the reference. */
.overview-grid{grid-template-columns:minmax(0,1fr);gap:13px}
.room-card{padding:24px 28px}
.room-card-body{grid-template-columns:minmax(0,1.12fr) minmax(0,.88fr);gap:30px;margin-top:18px}
.regulation-preview{border-top:0;border-left:1px solid rgba(255,255,255,.17);padding:0 0 0 28px;grid-template-columns:1fr}
.room-card-head h3{font-size:1.34rem;font-weight:550;letter-spacing:-.025em}
.dark .room-card-head h3{color:#c7aa79;font-weight:550;text-shadow:0 1px 10px rgba(168,115,48,.14)}
.light .room-card-head h3{color:#795b32}
.top-menu{gap:2px;padding:4px 5px;border-radius:999px}
.dark .top-menu.glass{background:linear-gradient(105deg,rgba(107,103,92,.66),rgba(69,67,61,.68));border-color:rgba(234,219,192,.34);box-shadow:0 10px 28px rgba(0,0,0,.25),inset 0 1px rgba(255,255,255,.24)}
.top-menu-link,.top-menu-add{min-width:36px;width:36px;height:36px;min-height:36px;border-radius:50%;border:0;background:transparent;box-shadow:none}
.top-menu-link span,.top-menu-add>span:first-child{font-size:1.2rem;font-weight:400}
.top-menu-link.active{background:rgba(255,255,255,.12);border:0;box-shadow:none}
.top-menu-add{margin-left:2px;background:linear-gradient(145deg,#b27538,#c8873f);border:1px solid rgba(255,222,166,.62);box-shadow:inset 0 1px rgba(255,255,255,.25),0 3px 9px rgba(57,35,15,.22)}
.light .top-menu.glass{background:linear-gradient(105deg,rgba(226,222,211,.78),rgba(205,204,194,.83));border-color:rgba(255,255,255,.85)}
@media(max-width:1100px){.room-card-body{grid-template-columns:1fr;gap:12px}.regulation-preview{border-left:0;border-top:1px solid rgba(255,255,255,.17);padding:12px 0 0;grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:640px){.room-card{padding:15px}.regulation-preview{grid-template-columns:1fr}.top-menu{gap:1px;padding:4px}.top-menu-link,.top-menu-add{min-width:36px;width:36px;height:36px;min-height:36px}}
/* Compact regulation facts; keep metric sizes while giving numbers a lighter face. */
.regulation-preview{grid-template-columns:repeat(2,minmax(0,1fr));column-gap:20px;align-content:center}
.regulation-preview p,.exercise-line{grid-column:1/-1}
.preview-line{min-width:0;gap:8px}
.preview-line span{min-width:0}
.preview-line strong{flex:none;white-space:nowrap}
.exercise-line{border-top:1px solid rgba(255,255,255,.17);margin-top:4px;padding-top:8px}
.exercise-line strong{white-space:normal;text-align:right}
.live-metrics strong,.temperature-tile strong,.dark .live-metrics strong,.dark .temperature-tile strong{font-family:"Segoe UI Variable Display","Segoe UI Variable Text","Segoe UI",sans-serif;font-weight:400;letter-spacing:-.045em;font-variant-numeric:tabular-nums}
@media(max-width:640px){.regulation-preview{grid-template-columns:1fr}}
/* The three live values share one inset glass panel. */
.live-metrics{gap:0;overflow:hidden;border:1px solid rgba(183,149,96,.4);border-radius:16px;background:linear-gradient(135deg,rgba(255,255,255,.085),rgba(255,255,255,.035));box-shadow:inset 0 1px rgba(255,255,255,.12),inset 0 -1px rgba(0,0,0,.1)}
.dark .room-card .live-metrics>.valve-tile,.dark .room-card .live-metrics>.temperature-tile,.light .room-card .live-metrics>.valve-tile,.light .room-card .live-metrics>.temperature-tile{min-height:90px;padding:10px 13px;border:0;border-radius:0;background:transparent;box-shadow:none}
.live-metrics>button:not(:last-child){border-right:1px solid rgba(183,149,96,.39)!important}
.dark .room-card .live-metrics>.valve-tile.heating{background:linear-gradient(90deg,rgba(193,137,65,.14),rgba(193,137,65,.025))}
.dark .room-card .live-metrics>button:hover{background-color:rgba(255,255,255,.08);box-shadow:none}
.light .live-metrics{background:linear-gradient(135deg,rgba(255,255,255,.43),rgba(234,231,221,.38));border-color:rgba(133,101,56,.3)}
.light .room-card .live-metrics>.valve-tile.heating{background:linear-gradient(90deg,rgba(192,145,84,.15),transparent)}
.light .room-card .live-metrics>button:hover{background-color:rgba(255,255,255,.4);box-shadow:none}
.live-metrics>button:focus-visible{outline:2px solid #d9ad66;outline-offset:-3px;z-index:1}
@media(max-width:640px){.live-metrics>button.valve-tile:not(:last-child){border-right:0!important;border-bottom:1px solid rgba(183,149,96,.39)!important}.live-metrics>.temperature-tile:last-child{border-right:0!important}}
/* Unframed values, fine gold dividers and an understated valve gauge. */
.live-metrics,.light .live-metrics{overflow:visible;border:0;border-radius:0;background:transparent;box-shadow:none}
.dark .room-card .live-metrics>.valve-tile,.dark .room-card .live-metrics>.temperature-tile,.light .room-card .live-metrics>.valve-tile,.light .room-card .live-metrics>.temperature-tile{min-height:90px;padding:9px 14px;border:0;border-radius:11px;background:transparent;box-shadow:none}
.dark .room-card .live-metrics>.valve-tile.heating,.light .room-card .live-metrics>.valve-tile.heating{background:transparent}
.live-metrics>button:not(:last-child){border-right:1px solid rgba(194,156,91,.52)!important;border-radius:0}
.dark .room-card .live-metrics>button:not(:last-child),.light .room-card .live-metrics>button:not(:last-child){border-radius:0}
.dark .room-card .live-metrics>button:hover{background:rgba(255,255,255,.095);box-shadow:inset 0 1px rgba(255,255,255,.08)}
.dark .room-card .live-metrics>.valve-tile.heating:hover{background:rgba(255,255,255,.095)}
.light .room-card .live-metrics>button:hover{background:rgba(255,255,255,.46);box-shadow:0 4px 14px rgba(55,65,61,.08)}
.light .room-card .live-metrics>.valve-tile.heating:hover{background:rgba(255,255,255,.46)}
.valve-tile .valve-progress{display:block;width:100%;height:3px;flex:none;margin-top:7px;border-radius:99px;background:rgba(185,146,81,.2);overflow:hidden}
.valve-progress i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#8d622c 0%,#c9994a 58%,#f0ce82 100%);box-shadow:0 0 8px rgba(227,180,92,.5);transition:width .45s ease}
.light .valve-tile .valve-progress{background:rgba(126,89,41,.17)}
@media(max-width:640px){.live-metrics>button.valve-tile:not(:last-child){border-right:0!important;border-bottom:1px solid rgba(194,156,91,.52)!important}.live-metrics>button.temperature-tile:nth-child(2){border-right:1px solid rgba(194,156,91,.52)!important}}
`;

customElements.define("room-thermostat-panel", RoomThermostatPanel);

