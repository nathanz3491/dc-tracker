/* Vendored for `tracker serve`. Identical to the design project's copy
 * except that the CDN URLs below are local paths: the console makes no
 * network requests, so three.js and the Census boundary TopoJSON are
 * served from /static/vendor/ alongside this file.
 */
/* <dc-map3d> — the same dataset as a drafting-table relief.
 *
 * State plates are extruded from the same us-atlas TopoJSON the flat map uses
 * (d3.geoAlbersUsa, real Census geometry), and each project rises as a column
 * whose height is its cited mw_planned. A project with no cited capacity gets a
 * thin dashed pin at zero height rather than an invented column — the same rule
 * the flat map's hollow ring follows.
 *
 * Attributes: encoding="phase|confidence" selected="<id>" autorotate
 * Events: dc-pick {detail:{id}}
 */
(function () {
  const THREE_URL = "/static/vendor/three.module.js";
  const ATLAS = "/static/vendor/us-states-10m.json";
  let threeP = null, atlasP = null;
  const getThree = () => (threeP = threeP || import(THREE_URL));
  const getAtlas = () => (atlasP = atlasP || fetch(ATLAS).then((r) => r.json()));

  const PHASE_TOKEN = {
    announced: "--chart-5", permitting: "--chart-1", construction: "--chart-3",
    operational: "--chart-2", paused: "--warning", cancelled: "--muted-foreground"
  };
  const CONF_TOKEN = { 0: "--muted-foreground", 1: "--warning", 2: "--chart-1", 3: "--success" };
  const W = 200, H = 120;

  class DCMap3D extends HTMLElement {
    static get observedAttributes() { return ["encoding", "selected"]; }

    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "block";
      this.style.position = "relative";
      this.style.width = "100%";
      this.style.height = "100%";
      this.style.minHeight = "320px";
      this.style.cursor = "grab";

      this._note = document.createElement("div");
      this._note.textContent = "building relief…";
      this._note.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:500 12px var(--font-mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted-foreground)";
      this.appendChild(this._note);

      this._label = document.createElement("div");
      this._label.style.cssText = "position:absolute;left:14px;bottom:14px;opacity:0;transition:opacity 140ms cubic-bezier(.4,0,.2,1);background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-pop);padding:9px 13px;font:500 12px/1.45 var(--font-sans,system-ui);color:var(--foreground);max-width:280px;pointer-events:none";
      this.appendChild(this._label);

      this.boot();
    }

    disconnectedCallback() {
      this._stop = true;
      this._ro && this._ro.disconnect();
      clearTimeout(this._settle);
      if (this._reset) window.removeEventListener("dc-reset-view", this._reset);
      if (this._renderer) { this._renderer.dispose(); this._renderer.domElement.remove(); }
    }

    attributeChangedCallback(n) {
      if (!this._scene) return;
      if (n === "selected") this.markSelection();
      else this.recolor();
    }

    tok(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888"; }

    async boot() {
      let THREE, topo;
      try { [THREE, topo] = await Promise.all([getThree(), getAtlas()]); }
      catch (e) { this._note.textContent = "3d relief unavailable offline"; return; }
      if (!window.DCTRACKER) await new Promise((r) => window.addEventListener("dctracker-ready", r, { once: true }));
      this.THREE = THREE;
      this._note.remove();

      const states = topojson.feature(topo, topo.objects.states);
      const projection = d3.geoAlbersUsa().fitExtent([[2, 2], [W - 2, H - 2]], states);
      this._projection = projection;

      const scene = new THREE.Scene();
      this._scene = scene;
      const camera = new THREE.PerspectiveCamera(38, 1, 1, 4000);
      this._camera = camera;
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
      renderer.domElement.style.display = "block";
      renderer.domElement.style.width = "100%";
      renderer.domElement.style.height = "100%";
      this.appendChild(renderer.domElement);
      this._renderer = renderer;

      scene.add(new THREE.HemisphereLight(0xfff6e6, 0x2a1f16, 1.05));
      const key = new THREE.DirectionalLight(0xffe9c4, 1.5);
      key.position.set(-90, 150, 90);
      scene.add(key);
      const rim = new THREE.DirectionalLight(0xdca75f, 0.5);
      rim.position.set(120, 60, -110);
      scene.add(rim);

      // --- state plates
      const plateGroup = new THREE.Group();
      scene.add(plateGroup);
      this._plates = [];
      const withProjects = new Set(window.DCTRACKER.projects.map((p) => p.state));
      const ABBR = { "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", 10: "DE", 11: "DC", 12: "FL", 13: "GA", 15: "HI", 16: "ID", 17: "IL", 18: "IN", 19: "IA", 20: "KS", 21: "KY", 22: "LA", 23: "ME", 24: "MD", 25: "MA", 26: "MI", 27: "MN", 28: "MS", 29: "MO", 30: "MT", 31: "NE", 32: "NV", 33: "NH", 34: "NJ", 35: "NM", 36: "NY", 37: "NC", 38: "ND", 39: "OH", 40: "OK", 41: "OR", 42: "PA", 44: "RI", 45: "SC", 46: "SD", 47: "TN", 48: "TX", 49: "UT", 50: "VT", 51: "VA", 53: "WA", 54: "WV", 55: "WI", 56: "WY" };

      states.features.forEach((f) => {
        const ab = ABBR[f.id];
        const active = withProjects.has(ab);
        const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
        const shapes = [];
        polys.forEach((rings) => {
          const outer = ring(rings[0], projection, THREE);
          if (!outer || outer.length < 3) return;
          const shape = new THREE.Shape(outer);
          for (let i = 1; i < rings.length; i++) {
            const hole = ring(rings[i], projection, THREE);
            if (hole && hole.length > 2) shape.holes.push(new THREE.Path(hole));
          }
          shapes.push(shape);
        });
        if (!shapes.length) return;
        const depth = active ? 3.2 : 1.4;
        const geo = new THREE.ExtrudeGeometry(shapes, { depth: depth, bevelEnabled: true, bevelThickness: 0.25, bevelSize: 0.25, bevelSegments: 1 });
        geo.rotateX(-Math.PI / 2);
        geo.translate(-W / 2, 0, -H / 2);
        const mat = new THREE.MeshStandardMaterial({ roughness: 0.82, metalness: 0.04, flatShading: false });
        const mesh = new THREE.Mesh(geo, mat);
        mesh.userData.active = active;
        mesh.userData.state = ab;
        plateGroup.add(mesh);
        this._plates.push(mesh);

        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geo, 25),
          new THREE.LineBasicMaterial({ transparent: true, opacity: active ? 0.5 : 0.22 })
        );
        plateGroup.add(edges);
        this._plates.push(edges);
      });

      // --- project columns
      this._marks = [];
      const markGroup = new THREE.Group();
      scene.add(markGroup);
      const maxMW = 1200;
      window.DCTRACKER.projects.forEach((p) => {
        if (p.lat == null) return;
        const xy = projection([p.lon, p.lat]);
        if (!xy) return;
        const x = xy[0] - W / 2, z = xy[1] - H / 2;
        const g = new THREE.Group();
        g.position.set(x, 3.2, z);
        const hasMW = p.mw_planned != null;
        const hgt = hasMW ? 4 + 30 * Math.sqrt(p.mw_planned / maxMW) : 3.5;
        const rad = hasMW ? 0.7 + 1.5 * Math.sqrt(p.mw_planned / maxMW) : 0.35;

        const colMat = new THREE.MeshStandardMaterial({ roughness: 0.35, metalness: 0.15, transparent: true, opacity: hasMW ? 0.92 : 0.55 });
        const col = new THREE.Mesh(new THREE.CylinderGeometry(rad, rad * 1.12, hgt, hasMW ? 18 : 6, 1, false), colMat);
        col.position.y = hgt / 2;
        g.add(col);

        const capMat = new THREE.MeshStandardMaterial({ roughness: 0.2, metalness: 0.3 });
        const cap = new THREE.Mesh(new THREE.SphereGeometry(hasMW ? rad * 1.25 : 0.6, 16, 12), capMat);
        cap.position.y = hgt + (hasMW ? rad * 0.5 : 0.4);
        g.add(cap);

        const blocking = p.risks.some((r) => r.status === "open" && r.severity === "blocking");
        let halo = null;
        if (blocking) {
          halo = new THREE.Mesh(
            new THREE.TorusGeometry(rad + 2.4, 0.22, 8, 40),
            new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.9 })
          );
          halo.rotation.x = -Math.PI / 2;
          halo.position.y = 0.4;
          g.add(halo);
        }

        const hit = new THREE.Mesh(new THREE.CylinderGeometry(Math.max(rad, 2.4), Math.max(rad, 2.4), hgt + 4, 8), new THREE.MeshBasicMaterial({ visible: false }));
        hit.position.y = (hgt + 4) / 2;
        hit.userData.pid = p.id;
        g.add(hit);

        markGroup.add(g);
        this._marks.push({ p: p, g: g, col: col, colMat: colMat, cap: cap, capMat: capMat, halo: halo, hasMW: hasMW, hgt: hgt, rad: rad });
      });

      // --- selection ring
      this._selRing = new THREE.Mesh(
        new THREE.RingGeometry(3.6, 4.4, 48),
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.95, side: THREE.DoubleSide })
      );
      this._selRing.rotation.x = -Math.PI / 2;
      this._selRing.visible = false;
      scene.add(this._selRing);

      this._raycaster = new THREE.Raycaster();
      this._pointer = new THREE.Vector2();
      this._orbit = { theta: -0.35, phi: 0.92, dist: 210, target: new THREE.Vector3(0, 6, 0) };
      // Settle rather than decorate: the slow reveal stops after six seconds, or
      // the moment the reader takes hold of it.
      this._auto = true;
      this._settle = setTimeout(() => { this._auto = false; }, 6000);
      this._reset = () => {
        this._orbit.theta = -0.35; this._orbit.phi = 0.92; this._orbit.dist = 210;
        this._orbit.target.set(0, 6, 0); this._auto = false;
      };
      window.addEventListener("dc-reset-view", this._reset);

      this.bindInput();
      this.recolor();
      this.markSelection();

      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this);
      this._mo = new MutationObserver(() => this.recolor());
      this._mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
      this.resize();

      const clock = new THREE.Clock();
      const tick = () => {
        if (this._stop) return;
        requestAnimationFrame(tick);
        const t = clock.getElapsedTime();
        if (this._auto && !this._dragging) this._orbit.theta += 0.0016;
        this._marks.forEach((m) => {
          if (m.halo) { const s = 1 + 0.14 * Math.sin(t * 2.2); m.halo.scale.set(s, s, 1); m.halo.material.opacity = 0.55 + 0.35 * (0.5 + 0.5 * Math.sin(t * 2.2)); }
          m.cap.position.y = m.hgt + (m.hasMW ? m.rad * 0.5 : 0.4) + Math.sin(t * 1.4 + m.p.id) * (m.hasMW ? 0.35 : 0.18);
        });
        if (this._selRing.visible) this._selRing.rotation.z = t * 0.5;
        this.place();
        renderer.render(scene, camera);
      };
      tick();
    }

    place() {
      const o = this._orbit;
      o.phi = Math.max(0.18, Math.min(1.45, o.phi));
      o.dist = Math.max(80, Math.min(430, o.dist));
      const d = o.dist * (this._fit || 1);
      const c = this._camera;
      c.position.set(
        o.target.x + d * Math.cos(o.phi) * Math.sin(o.theta),
        o.target.y + d * Math.sin(o.phi),
        o.target.z + d * Math.cos(o.phi) * Math.cos(o.theta)
      );
      c.lookAt(o.target);
    }

    resize() {
      if (!this._renderer) return;
      const w = this.clientWidth || 640, h = this.clientHeight || 380;
      this._renderer.setSize(w, h, false);
      const aspect = w / h;
      this._camera.aspect = aspect;
      this._camera.updateProjectionMatrix();
      // The relief is 200 units wide. In a narrow panel the horizontal field of
      // view collapses, so pull the camera back rather than cropping the country.
      this._fit = Math.max(1, Math.min(2.1, 1.75 / aspect));
    }

    recolor() {
      if (!this._plates) return;
      const T = this.THREE;
      const surface = new T.Color(this.tok("--surface"));
      const mutedC = new T.Color(this.tok("--muted"));
      const borderC = new T.Color(this.tok("--input"));
      const primary = new T.Color(this.tok("--primary"));
      this._plates.forEach((m) => {
        if (m.isLineSegments) { m.material.color.copy(borderC); return; }
        m.material.color.copy(m.userData.active ? surface.clone().lerp(primary, 0.14) : mutedC);
        m.material.emissive.copy(m.userData.active ? primary.clone().multiplyScalar(0.05) : new T.Color(0x000000));
      });
      const encoding = this.getAttribute("encoding") || "phase";
      const danger = new T.Color(this.tok("--danger"));
      this._marks.forEach((m) => {
        const tokName = encoding === "confidence" ? CONF_TOKEN[m.p.confidence] : (PHASE_TOKEN[m.p.phase] || "--chart-1");
        const c = new T.Color(this.tok(tokName));
        m.colMat.color.copy(c);
        m.colMat.emissive.copy(c.clone().multiplyScalar(0.28));
        m.capMat.color.copy(c.clone().lerp(new T.Color(0xffffff), 0.3));
        m.capMat.emissive.copy(c.clone().multiplyScalar(0.55));
        if (m.halo) m.halo.material.color.copy(danger);
      });
      if (this._selRing) this._selRing.material.color.copy(new T.Color(this.tok("--foreground")));
    }

    markSelection() {
      if (!this._selRing) return;
      const id = this.getAttribute("selected");
      const m = this._marks && this._marks.find((x) => String(x.p.id) === id);
      if (!m) { this._selRing.visible = false; return; }
      this._selRing.visible = true;
      this._selRing.position.set(m.g.position.x, 3.5, m.g.position.z);
    }

    bindInput() {
      const el = this._renderer.domElement;
      let last = null;
      el.addEventListener("pointerdown", (e) => { this._dragging = true; this._auto = false; last = [e.clientX, e.clientY]; el.setPointerCapture(e.pointerId); this.style.cursor = "grabbing"; });
      el.addEventListener("pointerup", (e) => { this._dragging = false; last = null; this.style.cursor = "grab"; });
      el.addEventListener("pointermove", (e) => {
        if (this._dragging && last) {
          this._orbit.theta -= (e.clientX - last[0]) * 0.006;
          this._orbit.phi += (e.clientY - last[1]) * 0.005;
          last = [e.clientX, e.clientY];
          return;
        }
        this.hover(e);
      });
      el.addEventListener("wheel", (e) => { e.preventDefault(); this._orbit.dist *= 1 + Math.sign(e.deltaY) * 0.08; }, { passive: false });
      el.addEventListener("click", () => { if (this._hoverId != null) this.dispatchEvent(new CustomEvent("dc-pick", { bubbles: true, composed: true, detail: { id: this._hoverId } })); });
      el.addEventListener("pointerleave", () => { this._hoverId = null; this._label.style.opacity = "0"; });
    }

    hover(e) {
      const r = this._renderer.domElement.getBoundingClientRect();
      this._pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      this._raycaster.setFromCamera(this._pointer, this._camera);
      const hits = this._raycaster.intersectObjects(this._marks.map((m) => m.g), true);
      const hit = hits.find((h) => h.object.userData.pid != null);
      const id = hit ? hit.object.userData.pid : null;
      if (id === this._hoverId) return;
      this._hoverId = id;
      this._renderer.domElement.style.cursor = id ? "pointer" : (this._dragging ? "grabbing" : "grab");
      if (id == null) { this._label.style.opacity = "0"; return; }
      const p = window.DCTRACKER.projects.find((x) => x.id === id);
      this._label.innerHTML = "";
      const t1 = document.createElement("div");
      t1.style.cssText = "font-weight:600";
      t1.textContent = p.company + " — " + p.name;
      const t2 = document.createElement("div");
      t2.style.cssText = "font-family:var(--font-mono);font-size:12px;color:var(--muted-foreground);margin-top:3px";
      t2.textContent = (p.city ? p.city + ", " : p.county ? p.county + " Co., " : "") + p.state + " · " + p.phase
        + " · " + (p.mw_planned != null ? p.mw_planned.toLocaleString() + " MW" : "no cited MW");
      this._label.appendChild(t1);
      this._label.appendChild(t2);
      this._label.style.opacity = "1";
    }
  }

  function ring(coords, projection, THREE) {
    const out = [];
    for (let i = 0; i < coords.length; i++) {
      const xy = projection(coords[i]);
      if (!xy) continue;
      out.push(new THREE.Vector2(xy[0], -xy[1]));
    }
    return out;
  }

  if (!customElements.get("dc-map3d")) customElements.define("dc-map3d", DCMap3D);
})();
