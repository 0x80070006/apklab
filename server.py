#!/usr/bin/env python3
"""APK Lab — décompilateur local d'APK / XAPK (jadx + apktool) avec interface web.

Lancer : python server.py   puis ouvrir http://127.0.0.1:8765
"""
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from paths import APP_DIR, DATA_DIR, TOOLS, WORK, STATIC

ROOT = APP_DIR
HOST = "127.0.0.1"
PORT = int(os.environ.get("APKLAB_PORT", "7341"))
JAVA_XMX = os.environ.get("APKLAB_XMX", "4g")
ANDROID_SDK = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") \
    or str(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Android" / "Sdk")
KEYSTORE = TOOLS / "apklab-debug.keystore"
KS_PASS = "apklab-debug"
KS_ALIAS = "apklab"

BUNDLE_EXT = {".xapk", ".apks", ".apkm", ".zip"}
DIRECT_EXT = {".apk", ".dex", ".jar", ".aar"}
TEXT_EXT = {
    ".java", ".kt", ".smali", ".xml", ".json", ".txt", ".properties", ".yml", ".yaml",
    ".html", ".htm", ".js", ".css", ".md", ".pro", ".cfg", ".ini", ".gradle", ".version",
}
MAX_VIEW = 4 * 1024 * 1024
MAX_SEARCH_FILE = 8 * 1024 * 1024
ANDROID = "{http://schemas.android.com/apk/res/android}"
PROGRESS_RE = re.compile(r"progress: (\d+) of (\d+)")

DANGEROUS_PERMS = {
    "READ_CALENDAR", "WRITE_CALENDAR", "CAMERA", "READ_CONTACTS", "WRITE_CONTACTS", "GET_ACCOUNTS",
    "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION", "RECORD_AUDIO",
    "READ_PHONE_STATE", "READ_PHONE_NUMBERS", "CALL_PHONE", "READ_CALL_LOG", "WRITE_CALL_LOG",
    "ADD_VOICEMAIL", "USE_SIP", "PROCESS_OUTGOING_CALLS", "ANSWER_PHONE_CALLS", "BODY_SENSORS",
    "SEND_SMS", "RECEIVE_SMS", "READ_SMS", "RECEIVE_WAP_PUSH", "RECEIVE_MMS",
    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO",
    "READ_MEDIA_AUDIO", "ACTIVITY_RECOGNITION", "POST_NOTIFICATIONS", "NEARBY_WIFI_DEVICES",
    "BLUETOOTH_SCAN", "BLUETOOTH_CONNECT", "BLUETOOTH_ADVERTISE", "UWB_RANGING",
    "SYSTEM_ALERT_WINDOW", "REQUEST_INSTALL_PACKAGES", "MANAGE_EXTERNAL_STORAGE",
    "BIND_ACCESSIBILITY_SERVICE", "BIND_DEVICE_ADMIN", "QUERY_ALL_PACKAGES", "READ_LOGS",
}


# --------------------------------------------------------------------------- outils

def find_java():
    exe = "java.exe" if os.name == "nt" else "java"
    cands = []
    if os.environ.get("JAVA_HOME"):
        cands.append(Path(os.environ["JAVA_HOME"]) / "bin" / exe)
    if shutil.which("java"):
        cands.append(Path(shutil.which("java")))
    cands += [
        Path(r"C:\Program Files\Android\Android Studio\jbr\bin\java.exe"),
        Path(r"C:\Program Files\Android\Android Studio\jre\bin\java.exe"),
        Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin/java"),
        Path("/opt/android-studio/jbr/bin/java"),
    ]
    return next((str(c) for c in cands if c.is_file()), None)


def find_jadx():
    jars = sorted((TOOLS / "jadx" / "lib").glob("jadx-*-all.jar"))
    return str(jars[-1]) if jars else None


def find_apktool():
    p = TOOLS / "apktool.jar"
    return str(p) if p.exists() else None


def _java_sibling(name):
    java = find_java()
    if not java:
        return None
    exe = name + (".exe" if os.name == "nt" else "")
    p = Path(java).parent / exe
    return str(p) if p.is_file() else (shutil.which(name) or None)


def find_keytool():
    return _java_sibling("keytool")


def find_build_tool(name):
    """apksigner / zipalign depuis le build-tools le plus récent du SDK Android."""
    bt = Path(ANDROID_SDK) / "build-tools"
    exts = [".bat", ".exe", ""] if os.name == "nt" else ["", ".exe"]
    if bt.is_dir():
        for ver in sorted(bt.iterdir(), reverse=True):
            for ext in exts:
                p = ver / (name + ext)
                if p.is_file():
                    return str(p)
    found = shutil.which(name)
    return found


def find_apksigner():
    return find_build_tool("apksigner")


def find_zipalign():
    return find_build_tool("zipalign")


def ensure_keystore(log=lambda s: None):
    """Crée au besoin un keystore de debug (pour signer les APK recompilés)."""
    if KEYSTORE.exists():
        return str(KEYSTORE)
    kt = find_keytool()
    if not kt:
        raise RuntimeError("keytool introuvable (JDK)")
    KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
    log("Génération du keystore de debug…")
    args = [kt, "-genkeypair", "-v", "-keystore", str(KEYSTORE), "-alias", KS_ALIAS,
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
            "-storepass", KS_PASS, "-keypass", KS_PASS,
            "-dname", "CN=APK Lab Debug, OU=apklab, O=apklab, C=FR"]
    r = subprocess.run(args, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError("keytool a échoué : " + (r.stderr or r.stdout)[:400])
    return str(KEYSTORE)


def tool_status():
    vfile = TOOLS / "versions.json"
    versions = json.loads(vfile.read_text()) if vfile.exists() else {}
    return {
        "java": find_java(),
        "jadx": find_jadx() and (versions.get("jadx") or "installé"),
        "apktool": find_apktool() and (versions.get("apktool") or "installé"),
        "apksigner": find_apksigner(),
        "zipalign": find_zipalign(),
        "keytool": find_keytool(),
    }


# --------------------------------------------------------------------------- jobs

class Cancelled(Exception):
    pass


class Job:
    def __init__(self, id, name, options, created=None, state="queued", step="", finished=None,
                 info=None, warnings=0, apks=None, rebuild=None, **_):
        self.id = id
        self.name = name
        self.options = options
        self.created = created or time.time()
        self.state = state  # queued | running | done | error | cancelled
        self.step = step
        self.finished = finished
        self.info = info
        self.warnings = warnings
        self.apks = apks or []
        self.rebuild = rebuild  # {state, step, apk, error, signed, ...}
        self.proc = None
        self.cancel_requested = False
        self.lock = threading.Lock()

    @property
    def dir(self):
        return WORK / self.id

    @property
    def out(self):
        return self.dir / "out"

    @property
    def build_dir(self):
        return self.dir / "build"

    @property
    def log_path(self):
        return self.dir / "job.log"

    @property
    def rebuild_log_path(self):
        return self.dir / "rebuild.log"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "options": self.options, "created": self.created,
            "state": self.state, "step": self.step, "finished": self.finished, "info": self.info,
            "warnings": self.warnings, "apks": self.apks, "rebuild": self.rebuild,
        }

    def save(self):
        (self.dir / "job.json").write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                                           encoding="utf-8")

    def log(self, line):
        with self.lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.save()


JOBS: dict[str, Job] = {}
EXPORT_LOCK = threading.Lock()
QUEUE: "queue.Queue[Job]" = queue.Queue()


def load_jobs():
    WORK.mkdir(exist_ok=True)
    for d in WORK.iterdir():
        f = d / "job.json"
        if not f.exists():
            continue
        try:
            job = Job(**json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
        if job.state in ("queued", "running"):
            job.state, job.step = "error", "interrompu (serveur redémarré)"
            job.save()
        if job.rebuild and job.rebuild.get("state") == "running":
            job.rebuild["state"] = "error"
            job.rebuild["error"] = "interrompu (serveur redémarré)"
            job.save()
        JOBS[job.id] = job


def run_cmd(job, args, label):
    job.set(step=label)
    job.log(f"\n=== {label}\n$ {' '.join(args)}")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(job.dir),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    job.proc = proc
    last_pct = None
    for raw in proc.stdout:
        for part in raw.split(b"\r"):
            line = part.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if m:
                # la progression de jadx va dans l'étape affichée, pas dans le journal
                pct = int(m.group(1)) * 100 // max(1, int(m.group(2)))
                if pct != last_pct:
                    last_pct = pct
                    job.set(step=f"{label} · {pct} %")
                continue
            job.log(line)
    code = proc.wait()
    job.proc = None
    if job.cancel_requested:
        raise Cancelled()
    job.log(f"--- code de sortie {code}")
    if code != 0:
        job.warnings += 1
    return code


def safe_extract(zf, member, dest_dir):
    target = (dest_dir / member).resolve()
    if dest_dir.resolve() not in target.parents:
        raise ValueError(f"chemin suspect dans l'archive : {member}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    return target


def extract_bundle(job, archive):
    """Extrait les APK d'un XAPK / APKS / APKM. Renvoie la liste, APK de base en premier."""
    dest = job.dir / "input" / "bundle"
    meta = job.out / "_bundle"
    apks, base_hint = [], None
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        for n in names:
            low = n.lower()
            if low.endswith(".apk"):
                apks.append(safe_extract(z, n, dest))
            elif low in ("manifest.json", "icon.png", "info.json", "toc.pb"):
                safe_extract(z, n, meta)
            elif low.endswith(".obb"):
                job.log(f"(OBB ignoré : {n})")
        if "manifest.json" in names:
            try:
                m = json.loads(z.read("manifest.json"))
                for s in m.get("split_apks", []):
                    if s.get("id") == "base":
                        base_hint = s.get("file")
                if not base_hint and m.get("package_name"):
                    base_hint = m["package_name"] + ".apk"
            except Exception:
                pass
    if not apks:
        raise ValueError("aucun APK trouvé dans l'archive")

    def has_dex(p):
        try:
            with zipfile.ZipFile(p) as z:
                return any(re.fullmatch(r"classes\d*\.dex", n) for n in z.namelist())
        except zipfile.BadZipFile:
            return False

    base = next((a for a in apks if base_hint and a.name == Path(base_hint).name), None)
    if base is None:
        base = next((a for a in apks if a.name == "base.apk"), None)
    if base is None:
        with_dex = [a for a in apks if has_dex(a)] or apks
        base = max(with_dex, key=lambda p: p.stat().st_size)
    apks.remove(base)
    return [base] + sorted(apks, key=lambda p: p.name)


def process(job):
    status = tool_status()
    java = status["java"]
    if not java:
        raise RuntimeError("Java introuvable (installe un JDK ou définis JAVA_HOME)")
    opts = job.options
    inp = job.dir / "input" / job.name
    job.out.mkdir(parents=True, exist_ok=True)

    ext = inp.suffix.lower()
    if ext in BUNDLE_EXT:
        job.set(step="extraction du bundle")
        apks = extract_bundle(job, inp)
        job.log("APK trouvés : " + ", ".join(a.name for a in apks))
    else:
        apks = [inp]
    job.set(apks=[a.stem for a in apks])

    for i, apk in enumerate(apks):
        dest = job.out / apk.stem
        tag = f"[{i + 1}/{len(apks)}] {apk.name}"
        is_split = i > 0
        if opts.get("jadx", True):
            if not status["jadx"]:
                raise RuntimeError("jadx absent : lance `python setup.py`")
            args = [java, f"-Xmx{JAVA_XMX}", "-cp", find_jadx(), "jadx.cli.JadxCLI",
                    "-d", str(dest / "jadx"), "--show-bad-code"]
            if opts.get("deobf"):
                args.append("--deobf")
            run_cmd(job, args + [str(apk)], f"jadx {tag}")
        if opts.get("apktool", True) and apk.suffix.lower() == ".apk" and not (is_split and not opts.get("splits_apktool")):
            if not status["apktool"]:
                raise RuntimeError("apktool absent : lance `python setup.py`")
            args = [java, f"-Xmx{JAVA_XMX}", "-jar", find_apktool(), "d", "-f", "--keep-broken-res",
                    "-p", str(TOOLS / "framework"), "-o", str(dest / "apktool"), str(apk)]
            run_cmd(job, args, f"apktool {tag}")

    job.set(step="analyse")
    job.info = analyze(job, apks)


def worker():
    while True:
        job = QUEUE.get()
        if job.cancel_requested or job.id not in JOBS:
            continue
        job.set(state="running", step="démarrage")
        try:
            process(job)
            job.set(state="done", step="terminé", finished=time.time())
        except Cancelled:
            job.set(state="cancelled", step="annulé", finished=time.time())
        except Exception as e:
            job.log(f"ERREUR : {e}")
            job.set(state="error", step=str(e), finished=time.time())


# --------------------------------------------------------------------------- recompilation + signature

REBUILDING: set[str] = set()
REBUILD_LOCK = threading.Lock()


def rlog(job, line):
    with open(job.rebuild_log_path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def run_build_cmd(job, args, label, env=None):
    job.rebuild["step"] = label
    job.save()
    rlog(job, f"\n=== {label}\n$ {' '.join(args)}")
    e = dict(os.environ)
    if env:
        e.update(env)
    proc = subprocess.run(args, capture_output=True, text=True, env=e,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.stdout:
        rlog(job, proc.stdout)
    if proc.stderr:
        rlog(job, proc.stderr)
    rlog(job, f"--- code de sortie {proc.returncode}")
    return proc.returncode


def rebuild_apk(job, opts):
    """apktool b → (zipalign) → apksigner. L'APK signé est déposé dans build/."""
    status = tool_status()
    java = status["java"]
    if not java:
        raise RuntimeError("Java introuvable")
    java_home = str(Path(java).parent.parent)
    env = {"JAVA_HOME": java_home, "PATH": str(Path(java).parent) + os.pathsep + os.environ.get("PATH", "")}

    stem = opts.get("target") or (job.info or {}).get("base") or (job.apks[0] if job.apks else None)
    src = job.out / stem / "apktool"
    if not src.is_dir():
        raise RuntimeError(f"pas de sortie apktool pour « {stem} » — relance la décompilation avec apktool activé")

    job.build_dir.mkdir(parents=True, exist_ok=True)
    unsigned = job.build_dir / f"{stem}-unsigned.apk"
    aligned = job.build_dir / f"{stem}-aligned.apk"
    signed = job.build_dir / f"{stem}-rebuilt-signed.apk"
    for p in (unsigned, aligned, signed):
        p.unlink(missing_ok=True)

    rlog(job, f"Recompilation de {stem} (source : {src})")
    code = run_build_cmd(job, [java, f"-Xmx{JAVA_XMX}", "-jar", find_apktool(), "b", str(src),
                               "-o", str(unsigned), "-f", "-p", str(TOOLS / "framework")],
                         "apktool build", env)
    if code != 0 or not unsigned.exists():
        raise RuntimeError("apktool build a échoué — voir le journal")

    to_sign = unsigned
    za = find_zipalign()
    if opts.get("zipalign", True) and za:
        code = run_build_cmd(job, [za, "-f", "-p", "4", str(unsigned), str(aligned)], "zipalign", env)
        if code == 0 and aligned.exists():
            to_sign = aligned
        else:
            rlog(job, "zipalign ignoré (échec)")

    if opts.get("sign", True):
        signer = find_apksigner()
        if not signer:
            raise RuntimeError("apksigner introuvable (build-tools du SDK Android)")
        ensure_keystore(lambda s: rlog(job, s))
        args = [signer, "sign", "--ks", str(KEYSTORE), "--ks-key-alias", KS_ALIAS,
                "--ks-pass", f"pass:{KS_PASS}", "--key-pass", f"pass:{KS_PASS}",
                "--out", str(signed), str(to_sign)]
        code = run_build_cmd(job, args, "apksigner sign", env)
        if code != 0 or not signed.exists():
            raise RuntimeError("apksigner a échoué — voir le journal")
        final, is_signed = signed, True
    else:
        final = to_sign.rename(job.build_dir / f"{stem}-rebuilt-unsigned.apk")
        is_signed = False

    return {"apk": final.name, "signed": is_signed, "size": final.stat().st_size,
            "zipaligned": to_sign == aligned}


def rebuild_worker(job, opts):
    try:
        job.rebuild = {"state": "running", "step": "démarrage", "started": time.time(), "target": opts.get("target")}
        job.save()
        job.rebuild_log_path.write_text("", encoding="utf-8")
        res = rebuild_apk(job, opts)
        job.rebuild = {"state": "done", "finished": time.time(), **res, "target": opts.get("target")}
        job.save()
    except Exception as e:
        rlog(job, f"ERREUR : {e}")
        job.rebuild = {"state": "error", "error": str(e), "finished": time.time(), "target": opts.get("target")}
        job.save()
    finally:
        with REBUILD_LOCK:
            REBUILDING.discard(job.id)


# --------------------------------------------------------------------------- analyse

def a(el, name):
    return el.get(ANDROID + name)


def analyze(job, apks):
    base = apks[0]
    info = {"base": base.stem, "files": {}}

    # Contenu brut des APK (bibliothèques natives, dex, frameworks)
    all_names = []
    for apk in apks:
        if apk.suffix.lower() not in (".apk", ".jar", ".aar"):
            continue
        try:
            with zipfile.ZipFile(apk) as z:
                for zi in z.infolist():
                    all_names.append(zi.filename)
                    if re.fullmatch(r"classes\d*\.dex", zi.filename):
                        info.setdefault("dex", []).append({"apk": apk.name, "name": zi.filename, "size": zi.file_size})
        except zipfile.BadZipFile:
            pass
    libs = {}
    for n in all_names:
        m = re.fullmatch(r"lib/([^/]+)/([^/]+\.so)", n)
        if m:
            libs.setdefault(m.group(1), []).append(m.group(2))
    info["native_libs"] = {k: sorted(set(v)) for k, v in sorted(libs.items())}

    names = set(all_names)
    sos = {n.rsplit("/", 1)[-1] for n in all_names if n.endswith(".so")}
    fw = []
    if "libflutter.so" in sos or "libapp.so" in sos:
        fw.append("Flutter (code Dart compilé dans libapp.so)")
    if "assets/index.android.bundle" in names or "libreactnativejni.so" in sos or "libreactnative.so" in sos:
        fw.append("React Native" + (" (Hermes)" if "libhermes.so" in sos else ""))
    if "libil2cpp.so" in sos:
        fw.append("Unity IL2CPP (voir global-metadata.dat)")
    elif "libunity.so" in sos:
        fw.append("Unity (Mono)")
    if "libmonodroid.so" in sos or any(n.startswith("assemblies/") for n in names):
        fw.append("Xamarin / .NET MAUI")
    if "assets/public/index.html" in names:
        fw.append("Capacitor / Ionic")
    elif "assets/www/index.html" in names:
        fw.append("Cordova")
    if any(n.startswith("kotlin/") or n.endswith(".kotlin_module") for n in names):
        fw.append("Kotlin")
    if any("androidx.compose" in n for n in names):
        fw.append("Jetpack Compose")
    if any(n.startswith("META-INF/") and n.split(".")[-1] in ("RSA", "DSA", "EC") for n in names):
        info["v1_signed"] = True
    info["frameworks"] = fw

    # Manifest décodé
    cands = [job.out / base.stem / "jadx" / "resources" / "AndroidManifest.xml",
             job.out / base.stem / "apktool" / "AndroidManifest.xml"]
    mpath = next((p for p in cands if p.exists()), None)
    if mpath:
        info["manifest_path"] = mpath.relative_to(job.out).as_posix()
        try:
            info.update(parse_manifest(mpath))
        except ET.ParseError as e:
            job.log(f"manifest illisible : {e}")
    app_name = (info.get("application") or {}).get("name") or ""
    if app_name.startswith("com.pairip.") or "libpairipcore.so" in sos:
        fw.append("PairIP (protection anti-altération Google Play)")

    yml = job.out / base.stem / "apktool" / "apktool.yml"
    if yml.exists():
        t = yml.read_text(encoding="utf-8", errors="replace")
        for key in ("versionCode", "versionName", "minSdkVersion", "targetSdkVersion"):
            m = re.search(rf"{key}:\s*'?([^'\n]+)'?", t)
            if m and not info.get(key):
                info[key] = m.group(1).strip()

    for kind, pattern in (("java", "*.java"), ("smali", "*.smali")):
        info["files"][kind] = sum(1 for _ in job.out.rglob(pattern))
    return info


def parse_manifest(path):
    root = ET.parse(path).getroot()
    info = {
        "package": root.get("package"),
        "versionCode": a(root, "versionCode"),
        "versionName": a(root, "versionName"),
    }
    sdk = root.find("uses-sdk")
    if sdk is not None:
        info["minSdkVersion"] = a(sdk, "minSdkVersion")
        info["targetSdkVersion"] = a(sdk, "targetSdkVersion")

    perms = []
    for tag in ("uses-permission", "uses-permission-sdk-23"):
        for p in root.iter(tag):
            name = a(p, "name") or ""
            short = name.rsplit(".", 1)[-1]
            perms.append({"name": name, "dangerous": name.startswith("android.permission.") and short in DANGEROUS_PERMS})
    info["permissions"] = sorted(perms, key=lambda p: (not p["dangerous"], p["name"]))
    info["declared_permissions"] = [
        {"name": a(p, "name"), "level": a(p, "protectionLevel")} for p in root.iter("permission")
    ]

    app = root.find("application")
    comps = []
    if app is not None:
        info["application"] = {
            "name": a(app, "name"),
            "debuggable": a(app, "debuggable"),
            "allowBackup": a(app, "allowBackup"),
            "usesCleartextTraffic": a(app, "usesCleartextTraffic"),
            "networkSecurityConfig": a(app, "networkSecurityConfig"),
        }
        for tag in ("activity", "activity-alias", "service", "receiver", "provider"):
            for c in app.iter(tag):
                filters = c.findall("intent-filter")
                actions = [a(x, "name") for f in filters for x in f.findall("action")]
                cats = [a(x, "name") for f in filters for x in f.findall("category")]
                exported = a(c, "exported")
                if exported is None:
                    exported = "implicite" if filters else "false"
                comps.append({
                    "type": tag, "name": a(c, "name"), "exported": exported,
                    "permission": a(c, "permission"), "actions": actions,
                    "authorities": a(c, "authorities"),
                    "launcher": "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in cats,
                })
        info["metadata"] = [{"name": a(m, "name"), "value": a(m, "value") or a(m, "resource")}
                            for m in app.findall("meta-data")]
    info["components"] = comps
    return info


# --------------------------------------------------------------------------- HTTP

def is_binary(path):
    with open(path, "rb") as f:
        return b"\0" in f.read(8192)


def guess_lang(path):
    return {".java": "java", ".kt": "kotlin", ".smali": "smali", ".xml": "xml", ".json": "json",
            ".yml": "yaml", ".yaml": "yaml", ".js": "javascript", ".html": "xml", ".htm": "xml",
            ".css": "css", ".properties": "properties", ".md": "markdown", ".pro": "properties",
            ".gradle": "groovy"}.get(path.suffix.lower(), "plaintext")


class SearchAborted(Exception):
    pass


# Contenu des fichiers du dernier job cherché : les recherches suivantes évitent le disque
SEARCH_CACHE = {"job": None, "files": {}, "size": 0}
SEARCH_CACHE_MAX = 400 * 1024 * 1024
SEARCH_CACHE_LOCK = threading.Lock()


def cached_read(job, p):
    with SEARCH_CACHE_LOCK:
        if SEARCH_CACHE["job"] != job.id:
            SEARCH_CACHE.update(job=job.id, files={}, size=0)
        raw = SEARCH_CACHE["files"].get(p)
    if raw is not None:
        return raw
    raw = p.read_bytes()
    with SEARCH_CACHE_LOCK:
        if SEARCH_CACHE["job"] == job.id and SEARCH_CACHE["size"] + len(raw) <= SEARCH_CACHE_MAX:
            SEARCH_CACHE["files"][p] = raw
            SEARCH_CACHE["size"] += len(raw)
    return raw


def drop_search_cache(job_id):
    with SEARCH_CACHE_LOCK:
        if SEARCH_CACHE["job"] == job_id:
            SEARCH_CACHE.update(job=None, files={}, size=0)


def search(job, q, regex, case, scope, limit=1000):
    flags = 0 if case else re.IGNORECASE
    source = q if regex else re.escape(q)
    pat = re.compile(source, flags)
    # pré-filtre sur les octets bruts : évite de décoder les milliers de fichiers sans résultat
    # (seulement si le motif se comporte pareil en octets : ASCII, sans classes dépendantes de l'Unicode)
    bpat = None
    if q.isascii() and not (regex and re.search(r"\\[wWbBdD]|(?<!\\)\.", q)):
        try:
            bpat = re.compile(source.encode("ascii"), flags)
        except re.error:
            pass
    # recherche littérale ASCII : simple sous-chaîne, bien plus rapide qu'une regex IGNORECASE
    needle = q.encode("ascii") if (not regex and q.isascii()) else None
    if needle is not None and not case:
        needle = needle.lower()
    with job.lock:
        job.search_gen = getattr(job, "search_gen", 0) + 1
        gen = job.search_gen
    scopes = {
        "java": lambda p: p.suffix in (".java", ".kt"),
        "smali": lambda p: p.suffix == ".smali",
        "res": lambda p: p.suffix not in (".java", ".kt", ".smali"),
    }
    keep = scopes.get(scope, lambda p: True)
    paths = [Path(d) / f for d, _, files in os.walk(job.out) for f in files]
    paths = [p for p in paths if p.suffix.lower() in TEXT_EXT and keep(p)]

    def scan(p):
        # exécuté dans un pool : la lecture disque libère le GIL
        if job.search_gen != gen:
            return None
        try:
            if p.stat().st_size > MAX_SEARCH_FILE:
                return None
            raw = cached_read(job, p)
        except OSError:
            return None
        if needle is not None:
            if needle not in (raw if case else raw.lower()):
                return None
        elif bpat is not None and not bpat.search(raw):
            return None
        text = raw.decode("utf-8", errors="replace")
        return text if pat.search(text) else None

    results, files_hit = [], 0
    with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as pool:
        for p, text in zip(paths, pool.map(scan, paths, chunksize=64)):
            if job.search_gen != gen:
                pool.shutdown(wait=False, cancel_futures=True)
                raise SearchAborted()
            if text is None:
                continue
            files_hit += 1
            rel = p.relative_to(job.out).as_posix()
            for i, line in enumerate(text.splitlines(), 1):
                m = pat.search(line)
                if m:
                    line = line.strip()
                    if len(line) > 300:
                        # ligne géante (JS minifié…) : extrait centré sur la correspondance
                        m = pat.search(line) or m
                        start = max(0, m.start() - 40)
                        line = ("…" if start else "") + line[start:start + 300] + "…"
                    results.append({"path": rel, "line": i, "text": line})
                    if len(results) >= limit:
                        with job.lock:
                            job.search_gen += 1  # stoppe les lectures restantes
                        return {"results": results, "files": files_hit, "truncated": True}
    return {"results": results, "files": files_hit, "truncated": False}


class Handler(BaseHTTPRequestHandler):
    server_version = "APKLab"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, download_name=None, ctype=None):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def error(self, code, msg):
        self.send_json({"error": msg}, code)

    def host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def mutating_ok(self):
        # en-tête personnalisé => le navigateur exige un preflight CORS que l'on ne valide jamais
        return self.headers.get("X-ApkLab") == "1"

    def job_or_404(self, jid):
        job = JOBS.get(jid)
        if not job:
            self.error(404, "job inconnu")
        return job

    def resolve(self, job, rel):
        base = job.out.resolve()
        p = (base / unquote(rel or "")).resolve()
        if p != base and base not in p.parents:
            return None
        return p

    # -- routing
    def handle_any(self, method):
        if not self.host_ok():
            return self.error(403, "hôte refusé")
        if method != "GET" and not self.mutating_ok():
            return self.error(403, "en-tête X-ApkLab manquant")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        try:
            if method == "GET" and (not parts or parts[0] == "static"):
                return self.static(parts)
            if parts[:1] != ["api"]:
                return self.error(404, "introuvable")
            parts = parts[1:]
            if parts == ["status"] and method == "GET":
                return self.send_json(tool_status())
            if parts == ["jobs"] and method == "GET":
                jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
                return self.send_json([j.to_dict() for j in jobs])
            if parts == ["upload"] and method == "PUT":
                return self.upload()
            if len(parts) >= 2 and parts[0] == "jobs":
                job = self.job_or_404(parts[1])
                if not job:
                    return
                action = parts[2] if len(parts) > 2 else ""
                return self.job_route(method, job, action, q)
            self.error(404, "route inconnue")
        except BrokenPipeError:
            pass
        except Exception as e:
            self.error(500, f"{type(e).__name__}: {e}")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def static(self, parts):
        rel = "/".join(parts[1:]) if parts else "index.html"
        base = STATIC.resolve()
        p = (base / rel).resolve()
        if base not in p.parents or not p.is_file():
            return self.error(404, "introuvable")
        ctype = mimetypes.guess_type(p.name)[0]
        if ctype and ctype.startswith("text/") or p.suffix == ".js":
            ctype = (ctype or "text/javascript") + "; charset=utf-8"
        self.send_file(p, ctype=ctype)

    def upload(self):
        name = Path(unquote(self.headers.get("X-Filename", ""))).name
        ext = Path(name).suffix.lower()
        if not name or ext not in BUNDLE_EXT | DIRECT_EXT:
            return self.error(400, f"extension non supportée : {ext or '?'}")
        try:
            options = json.loads(unquote(self.headers.get("X-Options", "{}")))
        except json.JSONDecodeError:
            options = {}
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return self.error(400, "fichier vide")
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = Job(jid, name, options)
        (job.dir / "input").mkdir(parents=True)
        remaining = length
        with open(job.dir / "input" / name, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            shutil.rmtree(job.dir, ignore_errors=True)
            return self.error(400, "envoi interrompu")
        job.log(f"Fichier reçu : {name} ({length / 1e6:.1f} Mo) — options {options}")
        job.save()
        JOBS[jid] = job
        QUEUE.put(job)
        self.send_json(job.to_dict())

    def job_route(self, method, job, action, q):
        if method == "GET" and action == "":
            return self.send_json(job.to_dict())
        if method == "GET" and action == "log":
            offset = int(q.get("offset", 0))
            data = job.log_path.read_bytes() if job.log_path.exists() else b""
            return self.send_json({"offset": len(data), "text": data[offset:].decode("utf-8", errors="replace")})
        if method == "GET" and action == "tree":
            d = self.resolve(job, q.get("path", ""))
            if not d or not d.is_dir():
                return self.send_json([])
            entries = []
            for e in os.scandir(d):
                p = Path(e.path)
                entries.append({"name": e.name, "path": p.relative_to(job.out.resolve()).as_posix(),
                                "dir": e.is_dir(), "size": 0 if e.is_dir() else e.stat().st_size})
            entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
            return self.send_json(entries)
        if method == "GET" and action == "file":
            p = self.resolve(job, q.get("path", ""))
            if not p or not p.is_file():
                return self.error(404, "fichier introuvable")
            size = p.stat().st_size
            res = {"path": q.get("path"), "size": size, "lang": guess_lang(p)}
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"):
                res["image"] = True
            elif is_binary(p):
                res["binary"] = True
                with open(p, "rb") as f:
                    res["hex"] = f.read(4096).hex()
            else:
                with open(p, "rb") as f:
                    raw = f.read(MAX_VIEW)
                res["content"] = raw.decode("utf-8", errors="replace")
                res["truncated"] = size > MAX_VIEW
            return self.send_json(res)
        if method == "GET" and action == "raw":
            p = self.resolve(job, q.get("path", ""))
            if not p or not p.is_file():
                return self.error(404, "fichier introuvable")
            ctype = "image/svg+xml" if p.suffix.lower() == ".svg" else None
            return self.send_file(p, download_name=p.name if q.get("dl") else None, ctype=ctype)
        if method == "GET" and action == "search":
            if not q.get("q"):
                return self.send_json({"results": [], "files": 0, "truncated": False})
            try:
                return self.send_json(search(job, q["q"], q.get("regex") == "1", q.get("case") == "1",
                                             q.get("scope", "all")))
            except re.error as e:
                return self.error(400, f"regex invalide : {e}")
            except SearchAborted:
                return self.error(409, "recherche remplacée par une plus récente")
        if method == "GET" and action == "export":
            if job.state != "done":
                return self.error(409, "décompilation non terminée")
            zpath = job.dir / "export.zip"
            with EXPORT_LOCK:
                if not zpath.exists() or zpath.stat().st_mtime < (job.finished or 0):
                    tmp = shutil.make_archive(str(job.dir / "export_tmp"), "zip", job.out)
                    os.replace(tmp, zpath)
            return self.send_file(zpath, download_name=f"{Path(job.name).stem}-decompile.zip")
        if method == "POST" and action == "rebuild":
            if job.state != "done":
                return self.error(409, "décompilation non terminée")
            with REBUILD_LOCK:
                if job.id in REBUILDING:
                    return self.error(409, "une recompilation est déjà en cours")
                REBUILDING.add(job.id)
            try:
                opts = json.loads(unquote(self.headers.get("X-Options", "{}")))
            except json.JSONDecodeError:
                opts = {}
            threading.Thread(target=rebuild_worker, args=(job, opts), daemon=True).start()
            return self.send_json({"ok": True})
        if method == "GET" and action == "rebuild":
            return self.send_json(job.rebuild or {"state": "none"})
        if method == "GET" and action == "rebuild-log":
            offset = int(q.get("offset", 0))
            data = job.rebuild_log_path.read_bytes() if job.rebuild_log_path.exists() else b""
            return self.send_json({"offset": len(data), "text": data[offset:].decode("utf-8", errors="replace")})
        if method == "GET" and action == "rebuild-download":
            if not (job.rebuild and job.rebuild.get("apk")):
                return self.error(404, "aucun APK recompilé")
            apk = (job.build_dir / job.rebuild["apk"]).resolve()
            if job.build_dir.resolve() not in apk.parents or not apk.is_file():
                return self.error(404, "APK introuvable")
            return self.send_file(apk, download_name=apk.name, ctype="application/vnd.android.package-archive")
        if method == "POST" and action == "cancel":
            job.cancel_requested = True
            if job.proc:
                job.proc.kill()
            if job.state == "queued":
                job.set(state="cancelled", step="annulé")
            return self.send_json(job.to_dict())
        if method == "POST" and action == "open":
            target = q.get("target", "explorer")
            folder = job.out if job.out.exists() else job.dir
            if target == "vscode":
                code = shutil.which("code")
                if not code:
                    return self.error(400, "commande `code` introuvable dans le PATH")
                subprocess.Popen([code, str(folder)], shell=os.name == "nt")
            elif os.name == "nt":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
            return self.send_json({"ok": True})
        if method == "DELETE" and action == "":
            job.cancel_requested = True
            if job.proc:
                job.proc.kill()
                time.sleep(0.5)
            JOBS.pop(job.id, None)
            drop_search_cache(job.id)
            shutil.rmtree(job.dir, ignore_errors=True)
            return self.send_json({"ok": True})
        self.error(404, "action inconnue")


def main():
    # Lancé via pythonw (sans console) : rediriger les sorties vers un fichier journal.
    if sys.stdout is None or sys.stderr is None:
        logf = open(DATA_DIR / "apklab.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or logf
        sys.stderr = sys.stderr or logf
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    st = tool_status()
    print("APK Lab")
    for k, v in st.items():
        print(f"  {k:8} {v or 'ABSENT'}")
    if not (st["jadx"] and st["apktool"]):
        print("  -> lance `python setup.py` pour installer jadx et apktool")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Interface : {url}  (Ctrl+C pour quitter)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
