"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtSize = (n) => n < 1024 ? `${n} o` : n < 1048576 ? `${(n / 1024).toFixed(1)} Ko` : `${(n / 1048576).toFixed(1)} Mo`;
const fmtDate = (t) => new Date(t * 1000).toLocaleString("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const short = (name) => (name || "").split(".").pop();

const STATE_LABEL = { queued: "en attente", running: "en cours", done: "terminé", error: "erreur", cancelled: "annulé" };
const LINE_H = 20;

const S = {
  jobs: [],
  current: null,
  tab: "overview",
  logOffset: 0,
  file: null,
  treeLoaded: false,
  compFilter: "all",
  lastState: {},
  lastRebuild: {},
  status: null,
  rebuildLogOffset: 0,
};

/* ------------------------------------------------------------------ API */

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-ApkLab": "1", ...(opts.headers || {}) } });
  const isJson = (r.headers.get("content-type") || "").includes("json");
  const data = isJson ? await r.json() : await r.text();
  if (!r.ok) throw new Error((isJson && data.error) || r.statusText);
  return data;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = "toast"), bad ? 5000 : 2500);
}

const jobUrl = (action = "", params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return `/api/jobs/${S.current}${action ? "/" + action : ""}${qs ? "?" + qs : ""}`;
};
const currentJob = () => S.jobs.find((j) => j.id === S.current);

/* ------------------------------------------------------------------ outils */

async function loadStatus() {
  try {
    const st = await api("/api/status");
    S.status = st;
    $("#tools").innerHTML = ["java", "jadx", "apktool"].map((k) => {
      const v = st[k];
      const label = k === "java" ? "java" : `${k} ${v && v !== "installé" ? v.replace(/^v/, "") : ""}`;
      return `<span class="pill ${v ? "" : "off"}" title="${esc(v || "absent — lance python setup.py")}">${esc(label.trim())}${v ? "" : " absent"}</span>`;
    }).join("");
  } catch (e) {
    $("#tools").innerHTML = `<span class="pill off">serveur injoignable</span>`;
  }
}

/* ------------------------------------------------------------------ envoi */

function upload(file) {
  const drop = $("#drop");
  const bar = $("#drop-progress i");
  const options = {
    jadx: $("#opt-jadx").checked,
    apktool: $("#opt-apktool").checked,
    deobf: $("#opt-deobf").checked,
    splits_apktool: $("#opt-splits").checked,
  };
  if (!options.jadx && !options.apktool) return toast("Active au moins jadx ou apktool", true);

  const xhr = new XMLHttpRequest();
  xhr.open("PUT", "/api/upload");
  xhr.setRequestHeader("X-ApkLab", "1");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  xhr.setRequestHeader("X-Options", encodeURIComponent(JSON.stringify(options)));
  drop.classList.add("busy");
  bar.style.width = "0";
  xhr.upload.onprogress = (e) => e.lengthComputable && (bar.style.width = `${(e.loaded / e.total) * 100}%`);
  xhr.onload = async () => {
    drop.classList.remove("busy");
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* réponse non JSON */ }
    if (xhr.status !== 200) return toast(data.error || "Échec de l'envoi", true);
    toast(`${file.name} envoyé, décompilation lancée`);
    await refreshJobs();
    selectJob(data.id);
  };
  xhr.onerror = () => { drop.classList.remove("busy"); toast("Échec de l'envoi", true); };
  xhr.send(file);
}

function setupDrop() {
  const drop = $("#drop");
  $("#file").addEventListener("change", (e) => {
    if (e.target.files[0]) upload(e.target.files[0]);
    e.target.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === "dragleave" && e.relatedTarget) return;
    drop.classList.remove("over");
  }));
  document.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) upload(f);
  });
}

/* ------------------------------------------------------------------ jobs */

async function refreshJobs() {
  try {
    S.jobs = await api("/api/jobs");
  } catch (e) {
    return;
  }
  renderJobs();
  const job = currentJob();
  if (job) {
    const prev = S.lastState[job.id];
    renderHeader(job);
    if (prev && prev !== job.state && (job.state === "done" || job.state === "error")) {
      renderOverview(job);
      resetTree();
      if (S.tab === "files") S.treePromise = loadTree();
      toast(job.state === "done" ? `${job.name} décompilé` : `${job.name} : erreur`, job.state === "error");
    }
    // recompilation : suivre l'avancement et notifier la fin
    const rbState = job.rebuild?.state;
    const prevRb = S.lastRebuild[job.id];
    if (S.tab === "rebuild" && rbState === "running") renderRebuildSteps(job.rebuild.step);
    if (prevRb === "running" && rbState !== "running") {
      if (S.tab === "rebuild") renderRebuild();
      toast(rbState === "done" ? "APK recompilé" + (job.rebuild.signed ? " et signé" : "") : "Recompilation : échec", rbState !== "done");
    }
    S.lastRebuild[job.id] = rbState;
  }
  S.jobs.forEach((j) => (S.lastState[j.id] = j.state));
}

function renderJobs() {
  const ul = $("#jobs");
  if (!S.jobs.length) {
    ul.innerHTML = `<li class="none">Rien pour l'instant.</li>`;
    return;
  }
  ul.innerHTML = S.jobs.map((j) => {
    const pkg = j.info?.package;
    const meta = j.state === "done" ? (pkg || fmtDate(j.created)) : (j.state === "running" ? j.step : STATE_LABEL[j.state]);
    return `<li data-id="${esc(j.id)}" class="${j.id === S.current ? "on" : ""}">
      <span class="dot ${j.state}"></span><span class="nm" title="${esc(j.name)}">${esc(j.name)}</span>
      <span class="meta">${esc(meta)}</span></li>`;
  }).join("");
}

function selectJob(id) {
  if (!S.jobs.some((j) => j.id === id)) return;
  S.current = id;
  S.logOffset = 0;
  S.file = null;
  S.compFilter = "all";
  $("#log").innerHTML = "";
  $("#search-results").innerHTML = "";
  $("#empty").hidden = true;
  $("#job").hidden = false;
  resetTree();
  resetViewer();
  renderJobs();
  const job = currentJob();
  renderHeader(job);
  renderOverview(job);
  history.replaceState(null, "", "#" + id);
  showTab(job.state === "done" ? S.tab : "log");
}

function renderHeader(job) {
  $("#job-title").textContent = job.name;
  $("#job-dot").className = "dot " + job.state;
  const i = job.info || {};
  const bits = [i.package, i.versionName && `v${i.versionName}`, i.versionCode && `(${i.versionCode})`, STATE_LABEL[job.state], fmtDate(job.created)];
  $("#job-sub").textContent = bits.filter(Boolean).join(" · ");
  const active = job.state === "running" || job.state === "queued";
  $("#job-progress").hidden = !active;
  $("#job-step").textContent = job.state === "queued" ? "en attente…" : job.step;
  $("#btn-cancel").hidden = !active;
  $("#btn-export").setAttribute("aria-disabled", String(job.state !== "done"));
  $("#btn-export").href = job.state === "done" ? jobUrl("export") : "#";
  const w = $("#warn-count");
  w.hidden = !job.warnings;
  w.textContent = job.warnings;
  w.title = `${job.warnings} étape(s) avec un code de sortie non nul`;
}

/* ------------------------------------------------------------------ onglets */

function showTab(tab) {
  S.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $$(".panel").forEach((p) => (p.hidden = p.dataset.panel !== tab));
  if (tab === "files" && !S.treeLoaded) S.treePromise = loadTree();
  if (tab === "log") pollLog();
  if (tab === "search") $("#search-q").focus();
  if (tab === "rebuild") renderRebuild();
}

/* ------------------------------------------------------------------ recompiler */

function renderRebuild() {
  const box = $("#rebuild-panel");
  const job = currentJob();
  if (!job) return;
  const st = S.status || {};
  const rb = job.rebuild;
  const running = rb && rb.state === "running";

  // cibles recompilables : les APK dont apktool a produit une sortie
  const base = job.info?.base;
  const targets = (job.apks || []).filter((s) => s === base || job.options?.splits_apktool);
  if (!job.options?.apktool) {
    box.innerHTML = `<div class="rebuild"><div class="callout warn"><h3>Recompilation indisponible</h3>
      <p>apktool n'a pas été exécuté sur cette décompilation. Le smali et les ressources sont nécessaires pour reconstruire l'APK.</p>
      <p class="muted">Relance une décompilation de ce fichier avec l'option « Smali + res (apktool) » activée.</p></div></div>`;
    return;
  }

  const missing = [];
  if (!st.apktool) missing.push("apktool");
  if (!st.apksigner) missing.push("apksigner (build-tools du SDK Android)");
  if (!st.keytool) missing.push("keytool (JDK)");

  const prev = rb && rb.state === "done"
    ? `<div class="callout ok"><h3>✓ APK recompilé${rb.signed ? " et signé" : ""}</h3>
        <p><code>${esc(rb.apk)}</code> · ${fmtSize(rb.size)}${rb.zipaligned ? " · zipaligné" : ""}</p>
        <div class="dl"><a class="btn" id="rb-download">Télécharger l'APK</a></div>
        <p class="muted" style="margin-top:10px">Signé avec un certificat de debug généré localement. À installer via « sources inconnues » / <code>adb install</code> pour tes tests — ce n'est pas la signature de l'éditeur.</p></div>`
    : rb && rb.state === "error"
    ? `<div class="callout bad"><h3>Échec de la recompilation</h3><p>${esc(rb.error || "voir le journal")}</p></div>`
    : "";

  box.innerHTML = `<div class="rebuild">
    <p class="lead">Reconstruit l'APK à partir des sources apktool (smali + ressources), puis l'aligne et le signe avec un certificat de debug local. Idéal pour tester tes modifications sur un appareil.</p>
    ${missing.length ? `<div class="callout warn"><h3>Outils manquants</h3><p>${esc(missing.join(", "))}</p></div>` : ""}
    ${prev}
    ${targets.length > 1 ? `<div class="row"><label class="muted" for="rb-target">APK à recompiler</label>
      <select class="rebuild-select" id="rb-target">${targets.map((s) => `<option${s === base ? " selected" : ""}>${esc(s)}</option>`).join("")}</select></div>` : ""}
    <label class="toggle"><input type="checkbox" id="rb-zipalign" ${st.zipalign ? "checked" : "disabled"}><span class="sw"></span>
      <span class="lbl"><b>zipalign</b><small>${st.zipalign ? "aligne l'APK sur 4 octets (recommandé)" : "zipalign introuvable"}</small></span></label>
    <label class="toggle"><input type="checkbox" id="rb-sign" checked><span class="sw"></span>
      <span class="lbl"><b>Signer</b><small>signature de debug (sinon APK non signé, non installable tel quel)</small></span></label>
    <div class="rebuild-actions">
      <button class="btn btn-lg" id="rb-start" ${running || !st.apktool ? "disabled" : ""}>
        ${running ? `<span class="spinner"></span> Recompilation…` : (rb ? "Recompiler à nouveau" : "Recompiler + signer")}</button>
    </div>
    ${running ? `<div class="rebuild-steps" id="rb-steps"></div>` : ""}
    <pre class="rlog" id="rlog" ${running || (rb && rb.state === "error") ? "" : "hidden"}></pre>
  </div>`;

  if (rb && rb.state === "done") $("#rb-download").href = jobUrl("rebuild-download");
  $("#rb-start") && ($("#rb-start").onclick = startRebuild);
  if (running) {
    renderRebuildSteps(rb.step);
    S.rebuildLogOffset = 0;
    $("#rlog").textContent = "";
    pollRebuildLog();
  } else if (rb && rb.state === "error") {
    S.rebuildLogOffset = 0;
    $("#rlog").textContent = "";
    pollRebuildLog();
  }
}

function renderRebuildSteps(step) {
  const el = $("#rb-steps");
  if (!el) return;
  const steps = [["apktool build", "Recompilation"], ["zipalign", "Alignement"], ["apksigner sign", "Signature"]];
  const idx = steps.findIndex(([k]) => (step || "").startsWith(k));
  el.innerHTML = steps.map(([k, l], n) =>
    `<span class="st ${n < idx ? "ok" : n === idx ? "on" : ""}">${n < idx ? "✓ " : ""}${l}</span>`).join("");
}

async function startRebuild() {
  const opts = {
    target: $("#rb-target")?.value || undefined,
    zipalign: $("#rb-zipalign")?.checked ?? true,
    sign: $("#rb-sign")?.checked ?? true,
  };
  try {
    await api(jobUrl("rebuild"), { method: "POST", headers: { "X-Options": encodeURIComponent(JSON.stringify(opts)) } });
  } catch (e) {
    return toast(e.message, true);
  }
  const job = currentJob();
  if (job) job.rebuild = { state: "running", step: "démarrage" };
  renderRebuild();
}

async function pollRebuildLog() {
  if (!S.current || pollRebuildLog.busy) return;
  pollRebuildLog.busy = true;
  try {
    const id = S.current;
    const r = await api(jobUrl("rebuild-log", { offset: S.rebuildLogOffset || 0 }));
    if (id !== S.current) return;
    S.rebuildLogOffset = r.offset;
    const log = $("#rlog");
    if (log && r.text) {
      const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.textContent += r.text;
      if (stick) log.scrollTop = log.scrollHeight;
    }
  } catch { /* retry */ } finally {
    pollRebuildLog.busy = false;
  }
}

/* ------------------------------------------------------------------ aperçu */

function renderOverview(job) {
  const el = $("#overview");
  const i = job.info;
  if (!i) {
    el.innerHTML = job.state === "error"
      ? `<div class="card"><h2>Échec</h2><p>${esc(job.step)}</p><button class="linkish" data-goto="log">Voir le journal</button></div>`
      : `<p class="muted">L'aperçu apparaîtra à la fin de la décompilation.</p>`;
    return;
  }
  const app = i.application || {};
  const comps = i.components || [];
  const exported = comps.filter((c) => c.exported === "true" || c.exported === "implicite");
  const dangerous = (i.permissions || []).filter((p) => p.dangerous);
  const dexSize = (i.dex || []).reduce((a, d) => a + d.size, 0);

  const flag = (label, val, badWhen) => {
    if (val == null) return `<span class="tag">${label} : défaut</span>`;
    const bad = String(val) === badWhen;
    return `<span class="tag ${bad ? "coral" : "blue"}">${label} : ${esc(val)}</span>`;
  };

  const libs = Object.entries(i.native_libs || {});
  el.innerHTML = `
  <div class="grid">
    <div class="card wide">
      <div class="stats">
        <div class="stat c-java"><b>${(i.files?.java ?? 0).toLocaleString("fr")}</b><span>fichiers Java</span></div>
        <div class="stat c-smali"><b>${(i.files?.smali ?? 0).toLocaleString("fr")}</b><span>fichiers smali</span></div>
        <div class="stat c-xml"><b>${comps.length}</b><span>composants · ${exported.length} exportés</span></div>
        <div class="stat c-java"><b>${(i.permissions || []).length}</b><span>permissions · ${dangerous.length} sensibles</span></div>
        <div class="stat c-res"><b>${(i.dex || []).length}</b><span>dex · ${fmtSize(dexSize)}</span></div>
      </div>
    </div>

    <div class="card">
      <h2>Identité</h2>
      <dl class="kv">
        <dt>Package</dt><dd>${esc(i.package || "?")}</dd>
        <dt>Version</dt><dd>${esc(i.versionName || "?")} <span class="muted">(${esc(i.versionCode || "?")})</span></dd>
        <dt>SDK</dt><dd>min ${esc(i.minSdkVersion || "?")} · cible ${esc(i.targetSdkVersion || "?")}</dd>
        <dt>Application</dt><dd>${esc(app.name || "—")}</dd>
        <dt>APK</dt><dd>${(job.apks || []).map((a, n) => `<span class="tag ${n ? "" : "coral"}">${esc(a)}</span>`).join("")}</dd>
      </dl>
      ${i.manifest_path ? `<p><button class="linkish" data-open="${esc(i.manifest_path)}">Ouvrir AndroidManifest.xml</button></p>` : ""}
    </div>

    <div class="card">
      <h2>Drapeaux</h2>
      <div class="flags">
        ${flag("debuggable", app.debuggable, "true")}
        ${flag("allowBackup", app.allowBackup, "true")}
        ${flag("cleartext", app.usesCleartextTraffic, "true")}
        ${app.networkSecurityConfig ? `<span class="tag plum">networkSecurityConfig : ${esc(app.networkSecurityConfig)}</span>` : `<span class="tag">pas de networkSecurityConfig</span>`}
        ${i.v1_signed ? `<span class="tag">signature v1</span>` : ""}
      </div>
      <h2 style="margin-top:16px">Technos détectées</h2>
      <div>${(i.frameworks || []).length ? i.frameworks.map((f) => `<span class="tag blue">${esc(f)}</span>`).join("") : `<span class="muted">Java/Kotlin natif Android</span>`}</div>
    </div>

    <div class="card">
      <h2>Permissions <span class="n">${(i.permissions || []).length}</span></h2>
      <ul class="list">${(i.permissions || []).map((p) => `<li class="${p.dangerous ? "warn" : ""}">${p.dangerous ? "●" : "<span class='muted'>○</span>"} ${esc(p.name)}</li>`).join("") || "<li class='muted'>aucune</li>"}</ul>
      ${(i.declared_permissions || []).length ? `<h2 style="margin-top:14px">Déclarées</h2><ul class="list">${i.declared_permissions.map((p) => `<li>${esc(p.name)} <span class="muted">${esc(p.level || "")}</span></li>`).join("")}</ul>` : ""}
    </div>

    <div class="card">
      <h2>Bibliothèques natives <span class="n">${libs.reduce((a, [, v]) => a + v.length, 0)}</span></h2>
      ${libs.length ? libs.map(([abi, so]) => `<div style="margin-bottom:8px"><span class="tag plum">${esc(abi)}</span><ul class="list">${so.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>`).join("") : `<p class="muted">Aucune .so</p>`}
      ${(i.metadata || []).length ? `<h2 style="margin-top:14px">meta-data <span class="n">${i.metadata.length}</span></h2><ul class="list">${i.metadata.map((m) => `<li><span>${esc(m.name)}</span> <span class="muted">${esc(m.value || "")}</span></li>`).join("")}</ul>` : ""}
    </div>

    <div class="card wide">
      <h2>Composants <span class="n">${comps.length}</span></h2>
      <div class="filterbar" id="comp-filter">
        ${[["all", "Tous"], ["exported", "Exportés"], ["activity", "Activités"], ["service", "Services"], ["receiver", "Receivers"], ["provider", "Providers"]]
          .map(([k, l]) => `<button class="chipbtn ${S.compFilter === k ? "on" : ""}" data-f="${k}">${l}</button>`).join("")}
      </div>
      <div id="comp-table"></div>
    </div>
  </div>`;
  renderComponents(comps);
}

function renderComponents(comps) {
  const f = S.compFilter;
  const rows = comps.filter((c) => f === "all" ? true
    : f === "exported" ? c.exported !== "false"
    : f === "activity" ? c.type.startsWith("activity") : c.type === f);
  const typeTag = { activity: "coral", "activity-alias": "coral", service: "blue", receiver: "plum", provider: "mauve" };
  $("#comp-table").innerHTML = rows.length ? `<table class="comps"><thead><tr><th>Type</th><th>Nom</th><th>Exporté</th><th>Actions / détails</th></tr></thead><tbody>
    ${rows.slice(0, 600).map((c) => `<tr>
      <td><span class="tag ${typeTag[c.type] || ""}">${esc(c.type)}</span>${c.launcher ? ` <span class="tag blue">lanceur</span>` : ""}</td>
      <td class="mono">${esc(c.name)}</td>
      <td>${c.exported === "false" ? `<span class="muted">non</span>` : `<span class="tag ${c.exported === "true" ? "coral" : "mauve"}">${c.exported === "true" ? "oui" : "implicite"}</span>`}</td>
      <td class="mono muted">${esc([...(c.actions || []).map(short), c.authorities && `authorities=${c.authorities}`, c.permission && `perm=${c.permission}`].filter(Boolean).join(", "))}</td>
    </tr>`).join("")}</tbody></table>${rows.length > 600 ? `<p class="muted">… ${rows.length - 600} de plus</p>` : ""}`
    : `<p class="muted">Aucun composant pour ce filtre.</p>`;
}

/* ------------------------------------------------------------------ arborescence */

function resetTree() {
  S.treeLoaded = false;
  $("#tree").innerHTML = "";
}

function fileKind(name) {
  const ext = name.split(".").pop().toLowerCase();
  if (ext === "java" || ext === "kt") return "java";
  if (ext === "smali") return "smali";
  if (ext === "xml") return "xml";
  if (ext === "json" || ext === "yml") return "json";
  if (ext === "so") return "so";
  if (["png", "jpg", "jpeg", "webp", "gif", "svg"].includes(ext)) return "img";
  return "";
}

async function fetchDir(path) {
  return api(jobUrl("tree", { path }));
}

function renderEntries(entries) {
  const ul = document.createElement("ul");
  ul.innerHTML = entries.map((e) => `<li data-path="${esc(e.path)}" data-dir="${e.dir ? 1 : 0}">
    <div class="node ${e.dir ? "dir" : ""}"><span class="ic ${e.dir ? "" : fileKind(e.name)}"></span><span>${esc(e.name)}</span>${e.dir ? "" : `<span class="sz">${fmtSize(e.size)}</span>`}</div></li>`).join("");
  return ul;
}

async function loadTree() {
  if (!S.current) return;
  S.treeLoaded = true;
  const tree = $("#tree");
  try {
    const entries = await fetchDir("");
    tree.innerHTML = "";
    if (!entries.length) {
      tree.innerHTML = `<p class="muted" style="padding:8px">Pas encore de fichiers.</p>`;
      S.treeLoaded = false;
      return;
    }
    tree.appendChild(renderEntries(entries));
    // déplie l'APK de base (pas _bundle)
    const base = currentJob()?.info?.base;
    const first = (base && $(`li[data-path="${CSS.escape(base)}"]`, tree)) || $("li[data-dir='1']", tree);
    if (first) await toggleDir(first, true);
  } catch (e) {
    S.treeLoaded = false;
    toast(e.message, true);
  }
}

async function toggleDir(li, forceOpen = false) {
  const node = li.firstElementChild;
  const open = node.classList.contains("open");
  if (open && !forceOpen) {
    node.classList.remove("open");
    li.querySelector(":scope > ul")?.remove();
    return;
  }
  if (open) return;
  node.classList.add("open");
  const entries = await fetchDir(li.dataset.path);
  if (!node.classList.contains("open") || li.querySelector(":scope > ul")) return;
  li.appendChild(renderEntries(entries));
}

async function reveal(path) {
  if (!S.treeLoaded) S.treePromise = loadTree();
  await S.treePromise;
  const parts = path.split("/");
  for (let n = 1; n < parts.length; n++) {
    const p = parts.slice(0, n).join("/");
    const li = $(`#tree li[data-path="${CSS.escape(p)}"]`);
    if (!li) return;
    await toggleDir(li, true);
  }
  const target = $(`#tree li[data-path="${CSS.escape(path)}"] > .node`);
  if (target) {
    $$("#tree .node.on").forEach((x) => x.classList.remove("on"));
    target.classList.add("on");
    target.scrollIntoView({ block: "center" });
  }
}

/* ------------------------------------------------------------------ visionneuse */

function resetViewer() {
  S.file = null;
  $("#crumbs").textContent = "Choisis un fichier";
  $("#code").innerHTML = "";
  $("#btn-copy").disabled = true;
  $("#btn-raw").setAttribute("aria-disabled", "true");
}

async function openFile(path, line) {
  if (S.tab !== "files") showTab("files");
  $("#crumbs").textContent = path;
  $("#crumbs").title = path;
  const code = $("#code");
  code.innerHTML = `<div class="note">Chargement…</div>`;
  $("#btn-raw").href = jobUrl("raw", { path, dl: 1 });
  $("#btn-raw").setAttribute("aria-disabled", "false");
  let f;
  try {
    f = await api(jobUrl("file", { path }));
  } catch (e) {
    code.innerHTML = `<div class="note">${esc(e.message)}</div>`;
    return;
  }
  S.file = f;
  $("#btn-copy").disabled = f.content == null;
  if (f.image) {
    code.innerHTML = `<img alt="${esc(path)}" src="${esc(jobUrl("raw", { path }))}">`;
  } else if (f.binary) {
    const hex = f.hex.match(/.{1,32}/g) || [];
    code.innerHTML = `<div class="note">Fichier binaire · ${fmtSize(f.size)} — aperçu hexadécimal des 4 premiers Ko</div>
      <div class="hex">${hex.map((h, n) => `${(n * 16).toString(16).padStart(6, "0")}  ${esc(h.match(/../g).join(" "))}`).join("\n")}</div>`;
  } else {
    renderCode(f, line);
  }
  reveal(path).catch(() => {});
}

function renderCode(f, line) {
  const code = $("#code");
  const text = f.content;
  const nLines = text.split("\n").length;
  let html;
  if (window.hljs && text.length < 600_000 && hljs.getLanguage(f.lang)) {
    try { html = hljs.highlight(text, { language: f.lang, ignoreIllegals: true }).value; } catch { html = esc(text); }
  } else {
    html = esc(text);
  }
  const gutter = Array.from({ length: nLines }, (_, k) => k + 1).join("\n");
  code.innerHTML = `${f.truncated ? `<div class="note">Fichier tronqué à 4 Mo (${fmtSize(f.size)}) — utilise Télécharger pour la version complète.</div>` : ""}
    <div class="wrap"><div class="gutter">${gutter}</div><pre>${line ? `<div class="hl-line" style="top:${10 + (line - 1) * LINE_H}px"></div>` : ""}<code>${html}</code></pre></div>`;
  const place = () => {
    code.scrollTop = line ? Math.max(0, (line - 1) * LINE_H - code.clientHeight / 3) : 0;
    code.scrollLeft = 0;
  };
  place();
  // réappliqué après le rendu (police web chargée, panneau tout juste affiché)
  requestAnimationFrame(place);
  document.fonts?.ready.then(place);
}

/* ------------------------------------------------------------------ recherche */

const PRESETS = [
  ["URLs", "https?://[^\"'\\s<>]+", true],
  ["Clés / secrets", "(api[_-]?key|secret|token|password|bearer)", true],
  ["Firebase", "firebaseio\\.com|google_api_key|gcm_defaultSenderId", true],
  ["WebView JS", "addJavascriptInterface|setJavaScriptEnabled", true],
  ["SharedPreferences", "getSharedPreferences", false],
  ["Certificate pinning", "CertificatePinner|X509TrustManager|checkServerTrusted", true],
  ["Logs", "Log\\.[dviwe]\\(", true],
];

function setupSearch() {
  $("#presets").innerHTML = PRESETS.map(([l], n) => `<button class="chipbtn" type="button" data-p="${n}">${esc(l)}</button>`).join("");
  $("#presets").addEventListener("click", (e) => {
    const b = e.target.closest("[data-p]");
    if (!b) return;
    const [, q, rx] = PRESETS[+b.dataset.p];
    $("#search-q").value = q;
    $("#search-regex").checked = rx;
    runSearch();
  });
  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
}

async function runSearch() {
  const q = $("#search-q").value.trim();
  const box = $("#search-results");
  if (!q || !S.current) return;
  const regex = $("#search-regex").checked;
  const kase = $("#search-case").checked;
  box.innerHTML = `<div class="summary">Recherche…</div>`;
  const t0 = performance.now();
  const token = (runSearch.token = (runSearch.token || 0) + 1);
  let res;
  try {
    res = await api(jobUrl("search", { q, regex: regex ? 1 : 0, case: kase ? 1 : 0, scope: $("#search-scope").value }));
  } catch (e) {
    if (token === runSearch.token) box.innerHTML = `<div class="summary">${esc(e.message)}</div>`;
    return;
  }
  if (token !== runSearch.token) return; // une recherche plus récente a pris le relais
  let pat;
  try { pat = new RegExp(regex ? q : q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), kase ? "g" : "gi"); } catch { pat = null; }
  const mark = (s) => {
    if (!pat) return esc(s);
    let out = "", last = 0;
    for (const m of s.matchAll(pat)) {
      if (!m[0]) break;
      out += esc(s.slice(last, m.index)) + `<mark>${esc(m[0])}</mark>`;
      last = m.index + m[0].length;
    }
    return out + esc(s.slice(last));
  };
  const byFile = new Map();
  res.results.forEach((r) => (byFile.get(r.path) || byFile.set(r.path, []).get(r.path)).push(r));
  const secs = ((performance.now() - t0) / 1000).toFixed(1);
  box.innerHTML = `<div class="summary">${res.results.length}${res.truncated ? "+" : ""} résultats dans ${res.files} fichiers · ${secs} s</div>` +
    [...byFile].map(([path, rs]) => `<div class="rfile"><div class="rfile-h"><span class="ic ${fileKind(path)}"></span>${esc(path)}</div>
      ${rs.map((r) => `<div class="rline" data-path="${esc(r.path)}" data-line="${r.line}"><span class="ln">${r.line}</span><span class="tx">${mark(r.text)}</span></div>`).join("")}</div>`).join("");
}

/* ------------------------------------------------------------------ journal */

async function pollLog() {
  if (!S.current || pollLog.busy) return;
  pollLog.busy = true;
  try {
    const id = S.current;
    const r = await api(jobUrl("log", { offset: S.logOffset }));
    if (id !== S.current || !r.text) return;
    S.logOffset = r.offset;
    const log = $("#log");
    const panel = log.parentElement;
    const stick = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 30;
    log.insertAdjacentHTML("beforeend", r.text.split("\n").map((l) => {
      const cls = l.startsWith("===") ? "head" : l.startsWith("$ ") ? "cmd" : /\b(ERROR|ERREUR|Exception|FAILED)\b/i.test(l) ? "err" : "";
      return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
    }).join("\n"));
    if (stick) panel.scrollTop = panel.scrollHeight;
  } catch { /* réessai au prochain tick */ } finally {
    pollLog.busy = false;
  }
}

/* ------------------------------------------------------------------ événements */

function setupEvents() {
  $("#jobs").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) selectJob(li.dataset.id);
  });
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) showTab(b.dataset.tab);
  });
  $("#tree").addEventListener("click", (e) => {
    const node = e.target.closest(".node");
    if (!node) return;
    const li = node.parentElement;
    if (li.dataset.dir === "1") toggleDir(li).catch((err) => toast(err.message, true));
    else openFile(li.dataset.path);
  });
  $("#overview").addEventListener("click", (e) => {
    const f = e.target.closest("[data-f]");
    if (f) {
      S.compFilter = f.dataset.f;
      $$("#comp-filter .chipbtn").forEach((b) => b.classList.toggle("on", b === f));
      renderComponents(currentJob()?.info?.components || []);
    }
    const o = e.target.closest("[data-open]");
    if (o) openFile(o.dataset.open);
    const g = e.target.closest("[data-goto]");
    if (g) showTab(g.dataset.goto);
  });
  $("#search-results").addEventListener("click", (e) => {
    const r = e.target.closest(".rline");
    if (r) openFile(r.dataset.path, +r.dataset.line);
  });
  $("#btn-copy").addEventListener("click", async () => {
    if (S.file?.content == null) return;
    try { await navigator.clipboard.writeText(S.file.content); toast("Copié"); } catch { toast("Copie impossible", true); }
  });
  $("#btn-cancel").addEventListener("click", () => api(jobUrl("cancel"), { method: "POST" }).then(refreshJobs).catch((e) => toast(e.message, true)));
  $("#btn-open").addEventListener("click", () => api(jobUrl("open", { target: "explorer" }), { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#btn-vscode").addEventListener("click", () => api(jobUrl("open", { target: "vscode" }), { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#btn-export").addEventListener("click", () => toast("Préparation du zip…"));
  $("#btn-delete").addEventListener("click", async () => {
    const job = currentJob();
    if (!job || !confirm(`Supprimer « ${job.name} » et tous ses fichiers décompilés ?`)) return;
    try {
      await api(jobUrl(), { method: "DELETE" });
      S.current = null;
      $("#job").hidden = true;
      $("#empty").hidden = false;
      history.replaceState(null, "", location.pathname);
      await refreshJobs();
    } catch (e) { toast(e.message, true); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) && S.current) {
      e.preventDefault();
      showTab("search");
    }
  });
}

/* ------------------------------------------------------------------ smali */

function registerSmali() {
  if (!window.hljs) return;
  hljs.registerLanguage("smali", () => ({
    name: "smali",
    contains: [
      hljs.COMMENT("#", "$"),
      hljs.QUOTE_STRING_MODE,
      { className: "keyword", begin: /^\s*\.[a-z-]+/ },
      { className: "type", begin: /\[*L[\w/$<>-]+;/ },
      { className: "title", begin: /->[\w$<>]+/ },
      { className: "variable", begin: /\b[vp]\d+\b/ },
      { className: "number", begin: /-?\b0x[0-9a-fA-F]+[LtsS]?\b|-?\b\d+\b/ },
      { className: "meta", begin: /:[\w]+/ },
      { className: "built_in", begin: /^\s*[a-z][a-z0-9-]*(?:\/[a-z0-9-]+)*/ },
    ],
  }));
}

/* ------------------------------------------------------------------ init */

async function init() {
  registerSmali();
  setupDrop();
  setupSearch();
  setupEvents();
  loadStatus();
  await refreshJobs();
  const fromHash = location.hash.slice(1);
  if (fromHash) selectJob(fromHash);
  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (id && id !== S.current) selectJob(id);
  });
  let tick = 0;
  setInterval(() => {
    const rebuilding = S.jobs.some((j) => j.rebuild?.state === "running");
    const busy = S.jobs.some((j) => j.state === "running" || j.state === "queued");
    if (busy || rebuilding || ++tick % 6 === 0) refreshJobs();
    const job = currentJob();
    if (job && S.tab === "log" && (busy || job.state === "running")) pollLog();
    if (job && S.tab === "rebuild" && job.rebuild?.state === "running") pollRebuildLog();
  }, 1500);
}

init();
