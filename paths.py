"""Emplacements des fichiers d'APK Lab.

En développement, les outils et le workspace vivent à côté du code.
Une fois installé dans Program Files (lecture seule pour l'utilisateur), ils vont
dans un dossier inscriptible par utilisateur (%LOCALAPPDATA%\\APK Lab sous Windows).
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        test = p / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    # priorité à un emplacement explicite
    env = os.environ.get("APKLAB_DATA")
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    # en dev : à côté du code, si on peut y écrire
    if _writable(APP_DIR / "tools"):
        return APP_DIR
    # installé : dossier utilisateur
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "APK Lab"
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = data_dir()
TOOLS = DATA_DIR / "tools"
WORK = DATA_DIR / "workspace"
TOOLS.mkdir(parents=True, exist_ok=True)
