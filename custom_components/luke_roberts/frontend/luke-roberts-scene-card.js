/*
 * Luke Roberts scene editor card.
 *
 * Shipped with the luke_roberts integration, which loads it on every
 * dashboard. Talks to the integration over websocket commands:
 *   luke_roberts/scenes       static scenes with their light
 *   luke_roberts/preview      show a light on the lamp without saving it
 *   luke_roberts/end_preview  back to the selected scene
 *   luke_roberts/save_scene   overwrite a scene or create a new one
 *
 * A light is { kelvin, uplight: 16 x [saturation, value, brightness],
 * spots: 84 brightnesses } as stored in the lamp: an uplight segment with
 * saturation 0 is white and its value is a colour temperature, otherwise
 * the value is the hue (0..65535).
 */

const RINGS = [3, 6, 9, 12, 15, 18, 21];
const SPOTS = [];
RINGS.forEach((count, ring) => {
  for (let j = 0; j < count; j++) {
    const a = (-2 * Math.PI * j) / count - Math.PI / 2;
    const r = (ring + 1) / RINGS.length;
    SPOTS.push([r * Math.cos(a), r * Math.sin(a)]);
  }
});
const MIN_K = 2700;
const MAX_K = 4000;
const PREVIEW_INTERVAL = 200; // ms between live previews on the lamp

// --- colour helpers ----------------------------------------------------------

function kelvinRgb(k) {
  const t = Math.max(1000, Math.min(40000, k)) / 100;
  let r, g, b;
  if (t <= 66) {
    r = 255;
    g = 99.4708 * Math.log(t) - 161.1196;
    b = t <= 19 ? 0 : 138.5177 * Math.log(t - 10) - 305.0448;
  } else {
    r = 329.6987 * (t - 60) ** -0.1332;
    g = 288.1222 * (t - 60) ** -0.0755;
    b = 255;
  }
  return [r, g, b].map((v) => Math.max(0, Math.min(255, v)));
}

function hsvRgb(h, s) {
  const f = (n) => {
    const k = (n + h * 6) % 6;
    return 255 * (1 - s * Math.max(0, Math.min(k, 4 - k, 1)));
  };
  return [f(5), f(3), f(1)];
}

function rgbHs([r, g, b]) {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d > 0) {
    if (max === r) h = ((g - b) / d + 6) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
  }
  return [h / 6, max > 0 ? d / max : 0];
}

/** Display colour of an uplight segment [sat, value, bri] (full brightness). */
function segmentRgb([sat, value]) {
  return sat === 0 ? kelvinRgb(value) : hsvRgb(value / 65535, sat / 255);
}

const css = (c) => `rgb(${c.map((v) => Math.round(v)).join(",")})`;
const hex = (c) =>
  "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
const fromHex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const clone = (o) => JSON.parse(JSON.stringify(o));
const sameLight = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// --- spread: how strongly each part follows a change -------------------------

function falloff(d, radius) {
  if (d <= 1e-6) return 1;
  if (radius <= 0 || d >= radius) return 0;
  return 0.5 * (1 + Math.cos((Math.PI * d) / radius));
}

function weights(sel, spread) {
  const up = Array.from({ length: 16 }, (_, i) =>
    Math.max(
      0,
      ...[...sel.up].map((c) => {
        const d = Math.abs(i - c);
        return falloff(Math.min(d, 16 - d), (spread / 100) * 8);
      })
    )
  );
  const down = SPOTS.map((p, i) =>
    Math.max(
      0,
      ...[...sel.down].map((c) =>
        falloff(
          Math.hypot(p[0] - SPOTS[c][0], p[1] - SPOTS[c][1]),
          (spread / 100) * 2
        )
      )
    )
  );
  return { up, down };
}

/** Blend a segment towards a colour target ({white: k} or {h, s}). */
function blendColour(seg, target, w) {
  if (w >= 1) {
    return "white" in target
      ? [0, Math.round(target.white), seg[2]]
      : [Math.max(1, Math.round(target.s * 255)), Math.round(target.h * 65535) & 0xffff, seg[2]];
  }
  const from = segmentRgb(seg);
  const to = "white" in target ? kelvinRgb(target.white) : hsvRgb(target.h, target.s);
  const [h, s] = rgbHs(from.map((v, j) => v + (to[j] - v) * w));
  return [Math.max(1, Math.round(s * 255)), Math.round(h * 65535) & 0xffff, seg[2]];
}

function applyTargets(base, targets, sel, spread) {
  const light = clone(base);
  const w = weights(sel, spread);
  light.uplight = light.uplight.map((seg, i) => {
    if (!w.up[i]) return seg;
    let out = seg;
    if (targets.colour) out = blendColour(out, targets.colour, w.up[i]);
    if (targets.upBri != null)
      out = [out[0], out[1], Math.round(out[2] + (targets.upBri - out[2]) * w.up[i])];
    return out;
  });
  if (targets.downBri != null) {
    light.spots = light.spots.map((v, i) =>
      w.down[i] ? Math.round(v + (targets.downBri - v) * w.down[i]) : v
    );
  }
  if (targets.kelvin != null) light.kelvin = targets.kelvin;
  return light;
}

// --- drawing -----------------------------------------------------------------

const P = (c, r, a) => [c + r * Math.cos(a), c + r * Math.sin(a)];

function arc(c, r0, r1, a0, a1) {
  const p = [P(c, r1, a0), P(c, r1, a1), P(c, r0, a1), P(c, r0, a0)];
  return `M${p[0]} A${r1},${r1} 0 0 0 ${p[1]} L${p[2]} A${r0},${r0} 0 0 1 ${p[3]}Z`;
}

function editorSvg(light, sel, spread) {
  const S = 340;
  const c = S / 2;
  const w = weights(sel, spread);
  const spotColour = css(kelvinRgb(light.kelvin));
  let fills = "";
  let marks = "";
  for (let i = 0; i < 16; i++) {
    const a0 = (-2 * Math.PI * i) / 16 - Math.PI / 2;
    const a1 = (-2 * Math.PI * (i + 1)) / 16 - Math.PI / 2;
    const seg = light.uplight[i];
    const op = Math.max(0.08, Math.sqrt(seg[2] / 255)).toFixed(2);
    fills += `<path data-up="${i}" d="${arc(c, S * 0.365, S * 0.45, a0 - 0.004, a1 + 0.004)}" fill="${css(segmentRgb(seg))}" fill-opacity="${op}"/>`;
    // Selection on a track outside the ring: visible on any colour.
    if (w.up[i])
      marks += `<path d="${arc(c, S * 0.468, S * 0.49, a0 - 0.012, a1 + 0.012)}" fill="#fff" fill-opacity="${w.up[i].toFixed(2)}"/>`;
    if (sel.up.has(i)) {
      const [x, y] = P(c, S * 0.408, (a0 + a1) / 2);
      marks += `<circle cx="${x}" cy="${y}" r="4.5" fill="#fff" stroke="#000" stroke-width="2"/>`;
    }
  }
  let spots = "";
  let rings = "";
  SPOTS.forEach((p, i) => {
    const x = (c + p[0] * S * 0.305).toFixed(1);
    const y = (c + p[1] * S * 0.305).toFixed(1);
    const op = Math.max(0.07, Math.sqrt(light.spots[i] / 255)).toFixed(2);
    spots += `<circle data-down="${i}" cx="${x}" cy="${y}" r="7" fill="${spotColour}" fill-opacity="${op}"/>`;
    if (w.down[i]) {
      // White ring for the dark lamp, dark inner line for a bright spot.
      const o = w.down[i].toFixed(2);
      const picked = sel.down.has(i);
      rings += `<circle cx="${x}" cy="${y}" r="7.6" fill="none" stroke="#fff" stroke-opacity="${o}" stroke-width="${picked ? 2.4 : 1.6}"/><circle cx="${x}" cy="${y}" r="${picked ? 5.6 : 6.1}" fill="none" stroke="#000" stroke-opacity="${o}" stroke-width="1.2"/>`;
    }
  });
  return (
    `<rect width="${S}" height="${S}" rx="${S / 2}" fill="#000"/>` +
    fills +
    `<g pointer-events="none">${marks}</g>` +
    `<circle cx="${c}" cy="${c}" r="${S * 0.345}" fill="#0b0b0b" pointer-events="none"/>` +
    spots +
    `<g pointer-events="none">${rings}</g>`
  );
}

// --- the card ----------------------------------------------------------------

const STYLE = `
  :host { display: block; }
  .body { padding: 0 16px 16px; }
  .top { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  select, input[type=text] {
    font: inherit; padding: 6px 8px; border-radius: 6px; flex: 1; min-width: 0;
    border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color);
  }
  .layout { display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }
  .lamp { flex: 1 1 260px; max-width: 380px; margin: 0 auto; }
  .lamp svg { width: 100%; height: auto; display: block; touch-action: none; user-select: none; }
  .controls { flex: 1 1 240px; min-width: 220px; }
  .sec { border: 1px solid var(--divider-color); border-radius: 10px; padding: 8px 12px; margin-bottom: 10px; }
  h4 { margin: 0 0 6px; font-size: 12px; font-weight: 500; text-transform: uppercase;
       letter-spacing: .04em; color: var(--secondary-text-color); }
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin: 6px 0; }
  .lab { display: flex; justify-content: space-between; font-size: 12px; color: var(--secondary-text-color); }
  .hint { font-size: 12px; color: var(--secondary-text-color); }
  input[type=range] { width: 100%; accent-color: var(--primary-color); }
  input[type=color] { width: 44px; height: 30px; border: none; background: none; padding: 0; }
  button {
    font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 999px; cursor: pointer;
    border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
  }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button:disabled { opacity: .45; cursor: default; }
  .msg { font-size: 13px; margin-top: 6px; min-height: 1em; }
  .msg.error { color: var(--error-color, #db4437); }
  .hidden { display: none !important; }
  label.check { display: flex; gap: 6px; align-items: center; font-size: 13px; }
`;

class LukeRobertsSceneCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._scenes = [];
    this._sceneId = null;
    this._original = null; // light as stored in the lamp
    this._base = null; // light before the current change
    this._light = null; // light shown
    this._targets = {};
    this._history = [];
    this._sel = { up: new Set(), down: new Set() };
    this._spread = 0;
    this._live = true;
    this._previewing = false;
    this._sending = false;
    this._pendingPreview = null;
    this._busy = false;
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("Choose a Luke Roberts light");
    this._config = config;
    this._build();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this._config) this._load();
  }

  getCardSize() {
    return 9;
  }

  static getConfigForm() {
    return {
      schema: [
        {
          name: "entity",
          required: true,
          selector: { entity: { domain: "light", integration: "luke_roberts" } },
        },
      ],
    };
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.entities || {}).find(
      (id) => id.startsWith("light.") && hass.entities[id].platform === "luke_roberts"
    );
    return { entity: entity || "" };
  }

  disconnectedCallback() {
    if (this._previewing) this._endPreview();
  }

  // --- data --------------------------------------------------------------------

  async _ws(message) {
    return this._hass.callWS({ entity_id: this._config.entity, ...message });
  }

  async _load(keepScene) {
    this._message("Reading the scenes from the lamp…");
    try {
      const result = await this._ws({ type: "luke_roberts/scenes" });
      this._scenes = result.scenes;
      this._freeSlots = result.free_slots;
      const ids = this._scenes.map((s) => s.id);
      let id = keepScene ?? this._sceneId;
      if (!ids.includes(id)) id = ids.includes(result.active) ? result.active : ids[0];
      this._fillScenePicker();
      this._selectScene(id);
      this._message(
        result.stale
          ? `The lamp could not be read right now; showing the stored copy. (${result.stale})`
          : "",
        Boolean(result.stale)
      );
    } catch (err) {
      this._message(err.message || String(err), true);
    }
  }

  _selectScene(id) {
    const scene = this._scenes.find((s) => s.id === id);
    if (!scene) return;
    this._sceneId = id;
    this._original = { kelvin: scene.kelvin, uplight: scene.uplight, spots: scene.spots };
    this._light = clone(this._original);
    this._history = [];
    this._sel = { up: new Set(), down: new Set() };
    this._commit();
    this.$("scene").value = String(id);
    this._render();
  }

  get _dirty() {
    return this._light && !sameLight(this._light, this._original);
  }

  // --- editing -----------------------------------------------------------------

  _commit() {
    this._base = clone(this._light);
    this._targets = {};
  }

  _edit(key, value) {
    if (!Object.keys(this._targets).length) this._history.push(clone(this._base));
    this._targets[key] = value;
    this._apply();
  }

  _apply() {
    this._light = applyTargets(this._base, this._targets, this._sel, this._spread);
    this._render();
    this._preview();
  }

  _changeSelection(change) {
    if (Object.keys(this._targets).length) this._commit();
    change();
    this._render();
  }

  _undo() {
    if (Object.keys(this._targets).length) {
      this._history.pop();
      this._targets = {};
      this._light = clone(this._base);
    } else if (this._history.length) {
      this._light = this._history.pop();
      this._commit();
    }
    this._render();
    this._preview();
  }

  // --- the lamp ----------------------------------------------------------------

  /** Live preview: at most one write in flight, the newest light wins. */
  _preview() {
    if (!this._live) return;
    this._pendingPreview = clone(this._light);
    if (!this._sending) this._sendPreview();
  }

  async _sendPreview() {
    const light = this._pendingPreview;
    if (!light) return;
    this._pendingPreview = null;
    this._sending = true;
    this._previewing = true;
    const started = Date.now();
    try {
      await this._ws({ type: "luke_roberts/preview", light });
    } catch (err) {
      this._message(err.message || String(err), true);
    }
    const wait = Math.max(0, PREVIEW_INTERVAL - (Date.now() - started));
    setTimeout(() => {
      this._sending = false;
      if (this._pendingPreview) this._sendPreview();
    }, wait);
  }

  async _endPreview() {
    this._pendingPreview = null;
    if (!this._previewing) return;
    this._previewing = false;
    try {
      await this._ws({ type: "luke_roberts/end_preview" });
    } catch (err) {
      this._message(err.message || String(err), true);
    }
  }

  async _save(asNew) {
    const name = this.$("name").value.trim();
    if (asNew && !name) {
      this._message("Give the new scene a name.", true);
      return;
    }
    this._busy = true;
    this._render();
    this._message(asNew ? "Creating the scene…" : "Saving…");
    try {
      const light = this._light;
      const target = asNew ? { name } : { scene_id: this._sceneId };
      const result = await this._ws({ type: "luke_roberts/save_scene", light, ...target });
      if (!asNew) await this._endPreview();
      this._pendingPreview = null;
      this._previewing = false;
      this.$("newrow").classList.add("hidden");
      await this._load(result.scene_id);
      this._message(asNew ? `“${name}” was created in the lamp.` : "Saved in the lamp.");
    } catch (err) {
      this._message(err.message || String(err), true);
    }
    this._busy = false;
    this._render();
  }

  async _discard() {
    this._light = clone(this._original);
    this._history = [];
    this._commit();
    this._render();
    await this._endPreview();
    this._message("");
  }

  // --- DOM ---------------------------------------------------------------------

  $(id) {
    return this.shadowRoot.getElementById(id);
  }

  _message(text, error = false) {
    const el = this.$("msg");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("error", error);
  }

  _fillScenePicker() {
    const counts = {};
    this._scenes.forEach((s) => (counts[s.name] = (counts[s.name] || 0) + 1));
    this.$("scene").innerHTML = this._scenes
      .map((s) => {
        const label = counts[s.name] > 1 ? `${s.name} (${s.id})` : s.name;
        return `<option value="${s.id}">${label.replace(/[<&]/g, "")}</option>`;
      })
      .join("");
  }

  _build() {
    if (this.$("lamp")) return;
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card header="${this._config.title || "Scene editor"}">
        <div class="body">
          <div class="top">
            <select id="scene" aria-label="Scene"></select>
            <label class="check"><input type="checkbox" id="live" checked> Show on lamp</label>
          </div>
          <div class="layout">
            <div class="lamp"><svg id="lamp" viewBox="0 0 340 340"></svg>
              <div class="hint">Tap the outer ring (uplight) or the spots (downlight); drag to pick several.</div>
            </div>
            <div class="controls">
              <div class="sec">
                <h4>Selection</h4>
                <div class="lab"><span>Spread</span><span id="spreadText"></span></div>
                <div class="row"><span class="hint">Precise</span><input type="range" id="spread" min="0" max="100" value="0" style="flex:1"><span class="hint">Soft</span></div>
                <div class="row">
                  <button id="allUp">All uplight</button><button id="allDown">All downlight</button><button id="none">None</button>
                </div>
              </div>
              <div class="sec" id="secNone"><h4>Nothing picked</h4>
                <div class="hint">Pick uplight segments for colour and brightness, or downlight spots for brightness.</div></div>
              <div class="sec hidden" id="secUp"><h4>Uplight</h4>
                <div class="row">Colour <input type="color" id="colour"> <button id="white">White</button></div>
                <div class="row hidden" id="whiteRow" style="display:block">
                  <div class="lab"><span>White colour temperature</span><span id="whiteText"></span></div>
                  <input type="range" id="whiteK" min="${MIN_K}" max="${MAX_K}" step="50"></div>
                <div class="lab"><span>Brightness</span><span id="upBriText"></span></div>
                <input type="range" id="upBri" min="0" max="100">
              </div>
              <div class="sec hidden" id="secDown"><h4>Downlight</h4>
                <div class="lab"><span>Brightness</span><span id="downBriText"></span></div>
                <input type="range" id="downBri" min="0" max="100">
              </div>
              <div class="sec"><h4>Whole scene</h4>
                <div class="lab"><span>Downlight colour temperature</span><span id="kelvinText"></span></div>
                <input type="range" id="kelvin" min="${MIN_K}" max="${MAX_K}" step="50">
                <div class="hint">All spots share one colour temperature.</div>
              </div>
              <div class="row">
                <button id="undo">Undo</button>
                <button id="discard">Discard</button>
                <button id="saveNew">Save as new…</button>
                <button id="save" class="primary">Save</button>
              </div>
              <div class="row hidden" id="newrow">
                <input type="text" id="name" maxlength="20" placeholder="Name of the new scene">
                <button id="create" class="primary">Create</button>
              </div>
              <div class="msg" id="msg"></div>
            </div>
          </div>
        </div>
      </ha-card>`;
    this._wire();
  }

  _wire() {
    const on = (id, event, fn) => this.$(id).addEventListener(event, fn);
    on("scene", "change", async (e) => {
      const id = Number(e.target.value);
      if (this._dirty && !confirm("Discard the changes to this scene?")) {
        e.target.value = String(this._sceneId);
        return;
      }
      this._selectScene(id);
      if (this._previewing) this._preview();
    });
    on("live", "change", (e) => {
      this._live = e.target.checked;
      if (this._live) this._preview();
      else this._endPreview();
    });
    on("spread", "input", (e) => {
      this._spread = Number(e.target.value);
      this._apply();
    });
    on("allUp", "click", () =>
      this._changeSelection(() => {
        this._sel = { up: new Set([...Array(16).keys()]), down: new Set() };
      })
    );
    on("allDown", "click", () =>
      this._changeSelection(() => {
        this._sel = { up: new Set(), down: new Set([...Array(84).keys()]) };
      })
    );
    on("none", "click", () =>
      this._changeSelection(() => {
        this._sel = { up: new Set(), down: new Set() };
      })
    );
    on("colour", "input", (e) => {
      const [h, s] = rgbHs(fromHex(e.target.value));
      this._edit("colour", { h, s });
    });
    on("white", "click", () => this._edit("colour", { white: Number(this.$("whiteK").value) || MIN_K }));
    on("whiteK", "input", (e) => this._edit("colour", { white: Number(e.target.value) }));
    on("upBri", "input", (e) => this._edit("upBri", Math.round(e.target.value * 2.55)));
    on("downBri", "input", (e) => this._edit("downBri", Math.round(e.target.value * 2.55)));
    on("kelvin", "input", (e) => this._edit("kelvin", Number(e.target.value)));
    on("undo", "click", () => this._undo());
    on("discard", "click", () => this._discard());
    on("save", "click", () => this._save(false));
    on("saveNew", "click", () => {
      this.$("newrow").classList.toggle("hidden");
      this.$("name").focus();
    });
    on("create", "click", () => this._save(true));

    // Picking on the lamp: tap toggles, drag continues; one layer at a time.
    const svg = this.$("lamp");
    let drag = null;
    // Pick by position, not by element: seams between segments and the gaps
    // between spots still pick the nearest part (easier on a phone).
    const hit = (e) => {
      const box = svg.getBoundingClientRect();
      const x = ((e.clientX - box.left) / box.width) * 340 - 170;
      const y = ((e.clientY - box.top) / box.height) * 340 - 170;
      const r = Math.hypot(x, y);
      if (r > 170) return null;
      if (r >= 340 * 0.355) {
        // Segments run counter-clockwise from the top.
        const turn = (((-Math.atan2(y, x) - Math.PI / 2) / (2 * Math.PI)) % 1 + 1) % 1;
        return ["up", Math.floor(turn * 16) % 16];
      }
      let best = 0;
      let bestD = Infinity;
      SPOTS.forEach((p, i) => {
        const d = Math.hypot(p[0] * 340 * 0.305 - x, p[1] * 340 * 0.305 - y);
        if (d < bestD) [best, bestD] = [i, d];
      });
      return bestD <= 16 ? ["down", best] : null;
    };
    svg.addEventListener("pointerdown", (e) => {
      const h = hit(e);
      if (!h) return;
      const [layer, i] = h;
      const add = !this._sel[layer].has(i);
      drag = { layer, add };
      try {
        svg.setPointerCapture(e.pointerId);
      } catch (_err) {
        // not a real pointer (tests)
      }
      this._changeSelection(() => {
        const other = layer === "up" ? "down" : "up";
        this._sel[other].clear();
        if (add) this._sel[layer].add(i);
        else this._sel[layer].delete(i);
      });
    });
    svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const h = hit(e);
      if (!h || h[0] !== drag.layer) return;
      const [layer, i] = h;
      if (drag.add !== this._sel[layer].has(i))
        this._changeSelection(() =>
          drag.add ? this._sel[layer].add(i) : this._sel[layer].delete(i)
        );
    });
    const stop = () => (drag = null);
    svg.addEventListener("pointerup", stop);
    svg.addEventListener("pointercancel", stop);
  }

  _setValue(id, value) {
    const el = this.$(id);
    // Leave a slider alone while it is being dragged.
    if (this.shadowRoot.activeElement !== el) el.value = value;
  }

  _render() {
    if (!this._light) return;
    const light = this._light;
    this.$("lamp").innerHTML = editorSvg(light, this._sel, this._spread);
    const up = [...this._sel.up][0];
    const down = [...this._sel.down][0];
    this.$("secUp").classList.toggle("hidden", up === undefined);
    this.$("secDown").classList.toggle("hidden", down === undefined);
    this.$("secNone").classList.toggle("hidden", up !== undefined || down !== undefined);
    if (up !== undefined) {
      const seg = light.uplight[up];
      const white = seg[0] === 0;
      if (!this._targets.colour) this._setValue("colour", hex(segmentRgb(seg)));
      this.$("whiteRow").classList.toggle("hidden", !white);
      if (white) {
        this._setValue("whiteK", seg[1]);
        this.$("whiteText").textContent = `${seg[1]} K`;
      }
      const pct = Math.round(seg[2] / 2.55);
      this._setValue("upBri", pct);
      this.$("upBriText").textContent = `${pct} %`;
    }
    if (down !== undefined) {
      const pct = Math.round(light.spots[down] / 2.55);
      this._setValue("downBri", pct);
      this.$("downBriText").textContent = `${pct} %`;
    }
    this._setValue("kelvin", light.kelvin);
    this.$("kelvinText").textContent = `${light.kelvin} K`;
    this._setValue("spread", this._spread);
    this.$("spreadText").textContent =
      this._spread === 0 ? "Precise" : this._spread < 35 ? "Small" : this._spread < 70 ? "Medium" : "Large";
    const dirty = this._dirty;
    this.$("save").disabled = this._busy || !dirty;
    this.$("discard").disabled = this._busy || (!dirty && !this._previewing);
    this.$("undo").disabled = this._busy || (!this._history.length && !Object.keys(this._targets).length);
    this.$("saveNew").disabled = this._busy || !this._freeSlots;
    this.$("create").disabled = this._busy;
  }
}

if (!customElements.get("luke-roberts-scene-card")) {
  customElements.define("luke-roberts-scene-card", LukeRobertsSceneCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "luke-roberts-scene-card",
    name: "Luke Roberts scene editor",
    description: "Edit the scenes stored in a Luke Roberts lamp, with a live preview on the lamp.",
  });
}
