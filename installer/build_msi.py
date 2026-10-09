#!/usr/bin/env python3
"""Construit l'installeur MSI d'APK Lab avec WiX v3 (téléchargé au besoin).

Usage : python installer/build_msi.py [version]
Sortie : installer/dist/APKLab-<version>.msi
"""
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIX_DIR = HERE / ".wix"
BUILD = HERE / "build"
DIST = HERE / "dist"
WIX_URL = "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"
VERSION = sys.argv[1] if len(sys.argv) > 1 else "1.0.0.0"


def ensure_wix():
    candle = WIX_DIR / "candle.exe"
    if candle.exists():
        return
    WIX_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = WIX_DIR / "wix314-binaries.zip"
    print("Téléchargement de WiX v3.14…")
    req = urllib.request.Request(WIX_URL, headers={"User-Agent": "apklab-build"})
    with urllib.request.urlopen(req, timeout=300) as r, open(zip_path, "wb") as f:
        f.write(r.read())
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(WIX_DIR)
    zip_path.unlink()
    print("WiX installé dans", WIX_DIR)


def run(args):
    print("$", " ".join(str(a) for a in args))
    r = subprocess.run(args, cwd=str(HERE))
    if r.returncode != 0:
        raise SystemExit(f"échec ({r.returncode}) : {args[0]}")


def main():
    if os.name != "nt":
        raise SystemExit("La construction du MSI doit se faire sous Windows.")
    ensure_wix()
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    candle = str(WIX_DIR / "candle.exe")
    light = str(WIX_DIR / "light.exe")
    wixobj = BUILD / "apklab.wixobj"
    msi = DIST / f"APKLab-{VERSION}.msi"

    run([candle, "-nologo", "-arch", "x64", f"-dVersion={VERSION}",
         "-ext", "WixUIExtension", "-ext", "WixUtilExtension", "-o", str(wixobj), "apklab.wxs"])

    base = [light, "-nologo", "-ext", "WixUIExtension", "-ext", "WixUtilExtension", "-sval",
            "-o", str(msi), str(wixobj)]
    # interface en français si disponible, sinon anglais
    try:
        run(base + ["-cultures:fr-FR"])
    except SystemExit:
        print("Localisation fr-FR indisponible, bascule en en-US.")
        run(base + ["-cultures:en-US"])

    print("\nMSI généré :", msi, f"({msi.stat().st_size / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
