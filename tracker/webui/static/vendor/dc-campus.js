/* Vendored for `tracker serve`. Identical to the design project's copy
 * except that the CDN URLs below are local paths: the console makes no
 * network requests, so three.js and the Census boundary TopoJSON are
 * served from /static/vendor/ alongside this file.
 */
/* <dc-campus project="1"> — a schematic 3D campus for one project.
 *
 * Not a rendering of the real site: it is a diagram built from the row's own
 * numbers, and it says so. Halls = mw_planned / 150 MW; solid halls = the
 * mw_built share; ghosted wireframe halls = planned and not yet built. The
 * substation is lit only when the power track has actually reached `energized`,
 * and turns danger-coloured while a transmission or grid_capacity risk is open —
 * which is the "finished shell waiting on a substation" case tracks.py exists to
 * surface.
 *
 * Attributes: project="<id>"
 */
(function () {
  const THREE_URL = "/static/vendor/three.module.js";
  let threeP = null;
  // A failed load is not cached. `p = p || import(...)` memoises the
  // *rejected* promise, so one transient failure — an expired session
  // 401ing the module fetch, a dropped connection — became permanent for
  // the life of the page, and the element went on reporting "unavailable
  // offline" long after the cause was gone.
  const getThree = () =>
    (threeP = threeP || import(THREE_URL).catch((e) => { threeP = null; throw e; }));

  class DCCampus extends HTMLElement {
    static get observedAttributes() { return ["project"]; }

    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "block";
      this.style.position = "relative";
      this.style.width = "100%";
      this.style.height = "100%";
      this.style.minHeight = "260px";
      this.style.cursor = "grab";

      this._note = document.createElement("div");
      this._note.textContent = "schematic loading…";
      this._note.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:500 12px var(--font-mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted-foreground)";
      this.appendChild(this._note);

      this._cap = document.createElement("div");
      this._cap.style.cssText = "position:absolute;left:12px;top:12px;display:flex;flex-direction:column;gap:3px;font:500 12px/1.6 var(--font-mono);letter-spacing:.09em;text-transform:uppercase;color:var(--muted-foreground);pointer-events:none";
      this.appendChild(this._cap);

      this.boot();
    }

    disconnectedCallback() {
      this._stop = true;
      this._ro && this._ro.disconnect();
      clearTimeout(this._settle);
      if (this._reset) window.removeEventListener("dc-reset-view", this._reset);
      if (this._renderer) { this._renderer.dispose(); this._renderer.domElement.remove(); }
    }

    attributeChangedCallback() { if (this._scene) this.build(); }

    tok(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#888"; }

    async boot() {
      let THREE;
      try { THREE = await getThree(); }
      catch (e) {
        // Recoverable, not terminal. Un-caching the promise above is only
        // half of it — without a way back, the element sits on this message
        // for the life of the page even once the cause is gone.
        this._note.textContent = "3d unavailable — tap to retry";
        this._note.style.cursor = "pointer";
        this._note.onclick = () => {
          this._note.onclick = null;
          this._note.style.cursor = "";
          this._note.textContent = "schematic loading…";
          this.boot();
        };
        return;
      }
      if (!window.DCTRACKER) await new Promise((r) => window.addEventListener("dctracker-ready", r, { once: true }));
      this.THREE = THREE;
      this._note.remove();

      const scene = new THREE.Scene();
      this._scene = scene;
      this._camera = new THREE.PerspectiveCamera(34, 1, 0.5, 800);
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
      renderer.domElement.style.display = "block";
      renderer.domElement.style.width = "100%";
      renderer.domElement.style.height = "100%";
      this.appendChild(renderer.domElement);
      this._renderer = renderer;

      scene.add(new THREE.HemisphereLight(0xfff4e2, 0x2b2018, 1.0));
      const key = new THREE.DirectionalLight(0xffeccd, 1.6);
      key.position.set(-40, 60, 34);
      scene.add(key);
      const fill = new THREE.DirectionalLight(0xdca75f, 0.45);
      fill.position.set(46, 22, -40);
      scene.add(fill);

      this._root = new THREE.Group();
      scene.add(this._root);

      this._orbit = { theta: -0.7, phi: 0.62, dist: 96, target: new THREE.Vector3(0, 3, -2) };
      this._auto = true;
      this._settle = setTimeout(() => { this._auto = false; }, 6000);
      this._reset = () => { this._orbit.theta = -0.7; this._orbit.phi = 0.62; this._auto = false; this.build(); };
      window.addEventListener("dc-reset-view", this._reset);
      this.bindInput();
      this.build();

      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this);
      this._mo = new MutationObserver(() => this.build());
      this._mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
      this.resize();

      const clock = new THREE.Clock();
      const tick = () => {
        if (this._stop) return;
        requestAnimationFrame(tick);
        const t = clock.getElapsedTime();
        if (this._auto && !this._dragging) this._orbit.theta += 0.0022;
        (this._pulse || []).forEach((o, i) => {
          o.material.opacity = o.userData.base + o.userData.amp * (0.5 + 0.5 * Math.sin(t * 1.8 + i));
        });
        (this._spin || []).forEach((o, i) => { o.rotation.y = t * (2 + i * 0.3); });
        this.place();
        renderer.render(scene, this._camera);
      };
      tick();
    }

    place() {
      const o = this._orbit;
      o.phi = Math.max(0.12, Math.min(1.4, o.phi));
      o.dist = Math.max(34, Math.min(220, o.dist));
      const d = o.dist * (this._fit || 1);
      this._camera.position.set(
        o.target.x + d * Math.cos(o.phi) * Math.sin(o.theta),
        o.target.y + d * Math.sin(o.phi),
        o.target.z + d * Math.cos(o.phi) * Math.cos(o.theta)
      );
      this._camera.lookAt(o.target);
    }

    resize() {
      if (!this._renderer) return;
      const w = this.clientWidth || 480, h = this.clientHeight || 300;
      this._renderer.setSize(w, h, false);
      const aspect = w / h;
      this._camera.aspect = aspect;
      this._camera.updateProjectionMatrix();
      this._fit = Math.max(1, 1.5 / aspect);
    }

    bindInput() {
      const el = this._renderer.domElement;
      let last = null;
      el.addEventListener("pointerdown", (e) => { this._dragging = true; this._auto = false; last = [e.clientX, e.clientY]; el.setPointerCapture(e.pointerId); this.style.cursor = "grabbing"; });
      el.addEventListener("pointerup", () => { this._dragging = false; last = null; this.style.cursor = "grab"; });
      el.addEventListener("pointermove", (e) => {
        if (!this._dragging || !last) return;
        this._orbit.theta -= (e.clientX - last[0]) * 0.007;
        this._orbit.phi += (e.clientY - last[1]) * 0.005;
        last = [e.clientX, e.clientY];
      });
      el.addEventListener("wheel", (e) => { e.preventDefault(); this._orbit.dist *= 1 + Math.sign(e.deltaY) * 0.09; }, { passive: false });
    }

    build() {
      const T = this.THREE;
      if (!T || !this._root) return;
      const root = this._root;
      while (root.children.length) root.remove(root.children[0]);
      this._pulse = [];
      this._spin = [];

      const id = parseInt(this.getAttribute("project") || "1", 10);
      const p = (window.DCTRACKER.projects || []).find((x) => x.id === id) || window.DCTRACKER.projects[0];
      if (!p) return;

      const mw = p.mw_planned;
      const halls = mw ? Math.max(1, Math.min(8, Math.round(mw / 150))) : 2;
      const builtHalls = mw && p.mw_built ? Math.min(halls, Math.max(0, Math.round((p.mw_built / mw) * halls))) : 0;
      const dead = p.phase === "cancelled" || p.phase === "paused";
      const energized = p.events.some((e) => e.event_type === "energized");
      const powerRisk = p.risks.some((r) => r.status === "open" && (r.category === "transmission" || r.category === "grid_capacity"));
      const shellDone = p.events.some((e) => e.event_type === "equipment_install");

      const C = {
        surface: new T.Color(this.tok("--surface")),
        muted: new T.Color(this.tok("--muted")),
        border: new T.Color(this.tok("--input")),
        ink: new T.Color(this.tok("--foreground")),
        primary: new T.Color(this.tok("--primary")),
        success: new T.Color(this.tok("--success")),
        danger: new T.Color(this.tok("--danger")),
        warning: new T.Color(this.tok("--warning")),
        dim: new T.Color(this.tok("--muted-foreground"))
      };

      // ground pad
      const padW = 12 + halls * 9, padD = 34;
      const pad = new T.Mesh(new T.BoxGeometry(padW, 0.7, padD),
        new T.MeshStandardMaterial({ color: C.muted, roughness: 0.95 }));
      pad.position.y = -0.35;
      root.add(pad);
      root.add(new T.LineSegments(new T.EdgesGeometry(pad.geometry),
        new T.LineBasicMaterial({ color: C.border, transparent: true, opacity: 0.7 })).translateY(-0.35));

      // access road
      const road = new T.Mesh(new T.BoxGeometry(padW * 0.92, 0.08, 3.4),
        new T.MeshStandardMaterial({ color: C.border, roughness: 1 }));
      road.position.set(0, 0.06, padD / 2 - 4.4);
      root.add(road);

      // data halls
      const hallW = 7, hallH = 4.6, hallD = 15;
      const startX = -((halls - 1) * 9) / 2;
      for (let i = 0; i < halls; i++) {
        const x = startX + i * 9;
        const isBuilt = i < builtHalls;
        const isShell = !isBuilt && (shellDone || p.phase === "construction") && i < builtHalls + 1;
        const g = new T.Group();
        g.position.set(x, 0, -2.5);

        if (isBuilt || isShell) {
          const mat = new T.MeshStandardMaterial({
            color: isBuilt ? C.surface.clone().lerp(C.primary, 0.16) : C.muted,
            roughness: 0.62, metalness: 0.06
          });
          const box = new T.Mesh(new T.BoxGeometry(hallW, hallH, hallD), mat);
          box.position.y = hallH / 2 + 0.05;
          g.add(box);
          g.add(new T.LineSegments(new T.EdgesGeometry(box.geometry),
            new T.LineBasicMaterial({ color: isBuilt ? C.primary : C.border, transparent: true, opacity: isBuilt ? 0.65 : 0.5 }))
            .translateY(hallH / 2 + 0.05));
          // roof cooling units
          for (let k = 0; k < 4; k++) {
            const cu = new T.Mesh(new T.BoxGeometry(2.2, 0.9, 2.2),
              new T.MeshStandardMaterial({ color: C.border, roughness: 0.55, metalness: 0.25 }));
            cu.position.set(0, hallH + 0.55, -5.2 + k * 3.5);
            g.add(cu);
            const fan = new T.Mesh(new T.TorusGeometry(0.75, 0.1, 6, 18),
              new T.MeshStandardMaterial({ color: isBuilt ? C.primary : C.dim, roughness: 0.4, metalness: 0.4 }));
            fan.rotation.x = -Math.PI / 2;
            fan.position.set(0, hallH + 1.05, -5.2 + k * 3.5);
            g.add(fan);
            if (isBuilt && !dead) this._spin.push(fan);
          }
          // lit racks along the long face
          if (isBuilt && !dead) {
            for (let k = 0; k < 6; k++) {
              const win = new T.Mesh(new T.PlaneGeometry(0.9, 1.5),
                new T.MeshBasicMaterial({ color: C.primary, transparent: true, opacity: 0.7 }));
              win.position.set(hallW / 2 + 0.02, 2.4, -5.8 + k * 2.3);
              win.rotation.y = Math.PI / 2;
              win.userData.base = 0.42; win.userData.amp = 0.34;
              g.add(win);
              this._pulse.push(win);
            }
          }
        } else {
          // planned, not built: wireframe footprint only
          const box = new T.BoxGeometry(hallW, hallH, hallD);
          const wf = new T.LineSegments(new T.EdgesGeometry(box),
            new T.LineDashedMaterial({ color: C.dim, transparent: true, opacity: 0.42, dashSize: 0.8, gapSize: 0.6 }));
          wf.computeLineDistances();
          wf.position.y = hallH / 2 + 0.05;
          g.add(wf);
          const foot = new T.Mesh(new T.BoxGeometry(hallW, 0.12, hallD),
            new T.MeshStandardMaterial({ color: C.muted, roughness: 1 }));
          foot.position.y = 0.12;
          g.add(foot);
        }
        root.add(g);
      }

      // substation
      const sub = new T.Group();
      sub.position.set(startX - 9.5, 0, -2);
      const subLive = energized && !powerRisk && !dead;
      const subCol = dead ? C.dim : powerRisk ? C.danger : subLive ? C.success : C.warning;
      const yard = new T.Mesh(new T.BoxGeometry(9, 0.3, 13),
        new T.MeshStandardMaterial({ color: C.muted, roughness: 1 }));
      yard.position.y = 0.2;
      sub.add(yard);
      for (let i = 0; i < 3; i++) {
        const tr = new T.Mesh(new T.BoxGeometry(2.4, 2.6, 2.4),
          new T.MeshStandardMaterial({ color: C.border, roughness: 0.5, metalness: 0.35 }));
        tr.position.set(-2, 1.5, -4 + i * 4);
        sub.add(tr);
        const bush = new T.Mesh(new T.CylinderGeometry(0.22, 0.22, 1.6, 8),
          new T.MeshStandardMaterial({ color: subCol, emissive: subCol.clone().multiplyScalar(subLive ? 0.7 : 0.2), roughness: 0.4 }));
        bush.position.set(-2, 3.4, -4 + i * 4);
        sub.add(bush);
      }
      // gantry + lines
      for (let i = 0; i < 2; i++) {
        const post = new T.Mesh(new T.CylinderGeometry(0.16, 0.16, 9, 6),
          new T.MeshStandardMaterial({ color: C.dim, roughness: 0.7, metalness: 0.3 }));
        post.position.set(2.6, 4.5, -4 + i * 8);
        sub.add(post);
      }
      const cross = new T.Mesh(new T.BoxGeometry(0.3, 0.3, 8.6),
        new T.MeshStandardMaterial({ color: C.dim, roughness: 0.7, metalness: 0.3 }));
      cross.position.set(2.6, 8.7, 0);
      sub.add(cross);
      for (let i = 0; i < 3; i++) {
        const line = new T.Mesh(new T.CylinderGeometry(0.05, 0.05, 26, 4),
          new T.MeshBasicMaterial({ color: subCol, transparent: true, opacity: subLive ? 0.8 : 0.35 }));
        line.rotation.z = Math.PI / 2;
        line.position.set(2.6 - 13, 8.4 - i * 0.9, -2.4 + i * 2.4);
        line.userData.base = subLive ? 0.5 : 0.2; line.userData.amp = subLive ? 0.35 : 0.12;
        sub.add(line);
        this._pulse.push(line);
      }
      root.add(sub);

      // caption
      this._cap.innerHTML = "";
      const lines = [
        "schematic · not the site",
        mw ? halls + " halls @ ~150 mw · " + builtHalls + " built" : "no cited capacity — 2 halls shown as outline",
        dead ? "phase " + p.phase + " — dark" : powerRisk ? "substation blocked" : subLive ? "substation live" : "substation not energized"
      ];
      lines.forEach((s, i) => {
        const d = document.createElement("div");
        d.textContent = s;
        if (i === 2) d.style.color = dead ? "var(--muted-foreground)" : powerRisk ? "var(--danger)" : subLive ? "var(--success)" : "var(--warning)";
        this._cap.appendChild(d);
      });

      this._orbit.target.set(0, 3, -2);
      const span = Math.max(padW, 40);
      this._orbit.dist = Math.max(60, span * 1.5);
    }
  }

  if (!customElements.get("dc-campus")) customElements.define("dc-campus", DCCampus);
})();
