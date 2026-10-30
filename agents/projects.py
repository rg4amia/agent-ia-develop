"""Détection et connexion de projets (dépôts Git locaux).

La détection est en lecture seule : on liste des dossiers contenant .git et on lit quelques
fichiers de configuration (composer.json, package.json, pubspec.yaml). Rien n'est enregistré
tant que l'utilisateur ne "connecte" pas explicitement un projet.
"""
import json
import os
import subprocess
import uuid
from pathlib import Path

from agents import db

MAX_DEPTH = 2
MAX_FOUND = 200
MAX_JSON_BYTES = 512_000
SKIP_DIRS = {"node_modules", "vendor", ".git", "storage", "build", "dist", ".venv", "__pycache__", "Library", "Applications"}
DEFAULT_ROOTS = "/Applications/MAMP/htdocs:~/Documents/PROJET PERSO"


class ProjectError(Exception):
    pass


def default_roots() -> list[Path]:
    raw = os.getenv("PROJECT_ROOTS", DEFAULT_ROOTS)
    return [Path(p).expanduser() for p in raw.split(":") if p.strip()]


def _read_json(path: Path) -> dict:
    try:
        if path.is_file() and path.stat().st_size <= MAX_JSON_BYTES:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _git(path: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect(path: Path) -> dict:
    """Analyse un dépôt : pile technique, commande de vérification, branche, état."""
    path = path.expanduser().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise ProjectError(f"{path} n'est pas un dépôt Git.")

    composer = _read_json(path / "composer.json")
    package = _read_json(path / "package.json")
    php_deps = {**composer.get("require", {}), **composer.get("require-dev", {})}
    js_deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    scripts = package.get("scripts", {}) if isinstance(package.get("scripts"), dict) else {}

    stacks: list[str] = []
    if (path / "artisan").is_file() and "laravel/framework" in php_deps:
        stacks.append("laravel")
    elif composer:
        stacks.append("php")
    if "inertiajs/inertia-laravel" in php_deps or any(k.startswith("@inertiajs/") for k in js_deps):
        stacks.append("inertia")
    if "react" in js_deps:
        stacks.append("react")
    if (path / "tsconfig.json").is_file():
        stacks.append("typescript")
    if package and not {"react", "next"} & set(js_deps):
        stacks.append("node")
    if "next" in js_deps:
        stacks.append("next")
    pubspec = path / "pubspec.yaml"
    if pubspec.is_file() and "flutter" in pubspec.read_text(encoding="utf-8", errors="ignore")[:20000]:
        stacks.append("flutter")
    if (path / "supabase").is_dir():
        stacks.append("supabase")
    if any((path / f).is_file() for f in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml")):
        stacks.append("docker")
    if (path / "pyproject.toml").is_file() or (path / "requirements.txt").is_file():
        stacks.append("python")

    # Seules les commandes de la liste blanche peuvent servir de vérification.
    if "laravel" in stacks:
        check = "php artisan test"
    elif "flutter" in stacks:
        check = "flutter test"
    elif "typescript" in stacks and package:
        check = "npx tsc --noEmit"
    elif "lint" in scripts:
        check = "npm run lint"
    elif "build" in scripts:
        check = "npm run build"
    else:
        check = None

    status = _git(path, "status", "--porcelain")
    return {
        "name": path.name,
        "path": str(path),
        "stacks": stacks,
        "check_command": check,
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD") or None,
        "dirty_files": len(status.splitlines()) if status else 0,
        "last_commit": _git(path, "log", "-1", "--format=%h %s") or None,
    }


def scan(roots: list[Path] | None = None) -> list[dict]:
    """Cherche les dépôts Git sous les racines (profondeur limitée), sans descendre dans un dépôt."""
    found: list[dict] = []
    for root in roots or default_roots():
        root = root.expanduser()
        if not root.is_dir():
            continue

        def walk(folder: Path, depth: int) -> None:
            if len(found) >= MAX_FOUND:
                return
            if (folder / ".git").exists():
                try:
                    found.append(detect(folder))
                except ProjectError:
                    pass
                return
            if depth >= MAX_DEPTH:
                return
            try:
                children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_DIRS)
            except OSError:
                return
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
    seen, unique = set(), []
    for p in found:
        if p["path"] not in seen:
            seen.add(p["path"])
            unique.append(p)
    return unique


# --- Projets connectés ----------------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    d["stacks"] = json.loads(d["stacks"])
    return d


def connected() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()
    return [_row(r) for r in rows]


def get(project_id: str) -> dict | None:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row(r) if r else None


def connect(path: str) -> dict:
    info = detect(Path(path))
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM projects WHERE path = ?", (info["path"],)).fetchone()
        pid = existing["id"] if existing else uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO projects (id, name, path, stacks, check_command, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET name=excluded.name, stacks=excluded.stacks, check_command=excluded.check_command",
            (pid, info["name"], info["path"], json.dumps(info["stacks"]), info["check_command"], db.now()),
        )
    return get(pid)


def disconnect(project_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount > 0
