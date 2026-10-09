#!/usr/bin/env python3
"""Télécharge les dernières versions officielles de jadx et apktool dans tools/.

Usage : python setup.py [--force]
"""
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from paths import TOOLS

UA = {"User-Agent": "apklab-setup", "Accept": "application/vnd.github+json"}


def latest_release(repo):
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(url, dest):
    print(f"  -> {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "apklab-setup"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    print(f"     {dest.stat().st_size / 1e6:.1f} Mo")


def pick_asset(release, pattern):
    for a in release["assets"]:
        if re.fullmatch(pattern, a["name"]):
            return a
    raise SystemExit(f"Aucun fichier '{pattern}' dans la release {release['tag_name']}")


def setup_jadx(force, versions):
    target = TOOLS / "jadx"
    if not force and list(target.glob("lib/jadx-*-all.jar")):
        print("jadx : déjà installé")
        return
    rel = latest_release("skylot/jadx")
    asset = pick_asset(rel, r"jadx-\d[\d.]*\.zip")
    print(f"jadx {rel['tag_name']}")
    archive = TOOLS / asset["name"]
    download(asset["browser_download_url"], archive)
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(target)
    archive.unlink()
    versions["jadx"] = rel["tag_name"]


def setup_apktool(force, versions):
    target = TOOLS / "apktool.jar"
    if not force and target.exists():
        print("apktool : déjà installé")
        return
    rel = latest_release("iBotPeaches/Apktool")
    asset = pick_asset(rel, r"apktool_\d[\d.]*\.jar")
    print(f"apktool {rel['tag_name']}")
    download(asset["browser_download_url"], target)
    versions["apktool"] = rel["tag_name"]


def main():
    force = "--force" in sys.argv
    TOOLS.mkdir(exist_ok=True)
    vfile = TOOLS / "versions.json"
    versions = json.loads(vfile.read_text()) if vfile.exists() else {}
    setup_jadx(force, versions)
    setup_apktool(force, versions)
    vfile.write_text(json.dumps(versions, indent=2))
    print("Outils prêts dans", TOOLS)


if __name__ == "__main__":
    main()
