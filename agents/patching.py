"""Application d'un diff sur une branche Git dédiée, dans un worktree séparé.

Le dossier de travail de l'utilisateur n'est jamais modifié : le correctif est appliqué et commité
dans data/worktrees/<run>-<nœud> sur la branche agent/<run>-<nœud>. Rien n'est poussé.
Tout appel à apply_to_branch doit venir d'une approbation humaine explicite.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from agents.db import DB_PATH
from agents.tools import IGNORED_DIRS, _is_sensitive

WORKTREES = DB_PATH.parent / "worktrees"
MAX_DIFF_CHARS = 200_000
MAX_FILES = 20
GIT_TIMEOUT = 60
# Hooks désactivés : un dépôt ne doit pas pouvoir exécuter de code pendant ces opérations.
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
_FORBIDDEN_HEADERS = ("rename from", "rename to", "copy from", "copy to", "old mode", "GIT binary patch", "Binary files")


class PatchError(Exception):
    pass


def extract_diff(text: str) -> str:
    """Récupère un diff unifié dans un bloc ```diff ou directement dans le texte."""
    pair = re.compile(r"^--- .+\n\+\+\+ .+$", re.M)
    for m in re.finditer(r"```[\w-]*[^\n]*\n(.*?)```", text, re.S):
        if pair.search(m.group(1)):
            return m.group(1).strip("\n") + "\n"
    m = pair.search(text)
    if m:
        start = text.rfind("diff --git", 0, m.start())
        begin = start if start != -1 and "\n\n" not in text[start:m.start()] else m.start()
        return text[begin:].strip("\n") + "\n"
    raise PatchError(
        "Aucun diff unifié trouvé (lignes '--- a/fichier' / '+++ b/fichier' avec des hunks '@@'). "
        "Demande à l'agent un diff au format `git diff`."
    )


def _clean(path: str) -> str:
    return path.split("\t")[0].strip().strip('"')


def validate_diff(diff: str) -> tuple[list[str], int]:
    """Vérifie le diff sans toucher au dépôt. Retourne (fichiers touchés, niveau -p)."""
    if len(diff) > MAX_DIFF_CHARS:
        raise PatchError("Diff trop volumineux.")
    for line in diff.splitlines():
        for bad in _FORBIDDEN_HEADERS:
            if line.startswith(bad):
                raise PatchError(f"Entête de diff non autorisé : {bad!r}")
        if line.startswith(("new file mode 12", "deleted file mode 12", "new mode")):
            raise PatchError("Lien symbolique ou changement de mode refusé.")
    raw = [_clean(l[4:]) for l in diff.splitlines() if l.startswith(("--- ", "+++ "))]
    raw = [p for p in raw if p != "/dev/null"]
    if not raw:
        raise PatchError("Diff sans fichier.")
    strip = 1 if all(p.startswith(("a/", "b/")) for p in raw) else 0
    files: list[str] = []
    for p in raw:
        rel = PurePosixPath(p)
        parts = rel.parts[strip:]
        if rel.is_absolute() or not parts or ".." in parts:
            raise PatchError(f"Chemin refusé : {p}")
        if parts[0] in IGNORED_DIRS or parts[0] == ".git" or ".git" in parts:
            raise PatchError(f"Dossier protégé : {'/'.join(parts)}")
        if _is_sensitive(Path(parts[-1])):
            raise PatchError(f"Fichier sensible : {'/'.join(parts)}")
        name = "/".join(parts)
        if name not in files:
            files.append(name)
    if len(files) > MAX_FILES:
        raise PatchError(f"Plus de {MAX_FILES} fichiers touchés.")
    return files, strip


def _git(cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*GIT, "-C", str(cwd), *args], input=stdin, capture_output=True, text=True, timeout=GIT_TIMEOUT
    )


def preflight(root: Path, diff: str) -> list[str]:
    """Contrôle que le diff s'applique à l'index courant (aucune écriture)."""
    files, strip = validate_diff(diff)
    res = _git(root, "apply", "--check", "--cached", "--recount", "--ignore-whitespace", f"-p{strip}", "-", stdin=diff)
    if res.returncode != 0:
        raise PatchError("Le diff ne s'applique pas au dépôt :\n" + (res.stderr.strip() or "erreur inconnue")[:1500])
    return files


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40] or "patch"


def apply_to_branch(root: Path, diff: str, run_id: str, node_id: str, message: str) -> dict:
    """Crée branche + worktree, applique le diff et commite. À n'appeler qu'après approbation."""
    files, strip = validate_diff(diff)
    tag = f"{run_id}-{_slug(node_id)}"
    branch, path = f"agent/{tag}", WORKTREES / tag
    WORKTREES.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PatchError(f"Le worktree existe déjà : {path}")

    added = _git(root, "worktree", "add", "-b", branch, str(path), "HEAD")
    if added.returncode != 0:
        raise PatchError("Création de la branche impossible : " + added.stderr.strip()[:500])
    try:
        for dep in ("vendor", "node_modules"):  # dépendances en lecture seule, pour pouvoir lancer des tests
            src = root / dep
            if src.is_dir() and not (path / dep).exists():
                os.symlink(src, path / dep)
        res = _git(path, "apply", "--index", "--recount", "--ignore-whitespace", f"-p{strip}", "-", stdin=diff)
        if res.returncode != 0:
            raise PatchError("Application du diff impossible : " + res.stderr.strip()[:1500])
        res = _git(
            path, "-c", "user.name=AEJ Agent", "-c", "user.email=agent@localhost",
            "commit", "-q", "--no-verify", "-m", message[:200] or "Correctif proposé par un agent",
        )
        if res.returncode != 0:
            raise PatchError("Commit impossible : " + (res.stderr or res.stdout).strip()[:500])
    except Exception:
        _git(root, "worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)
        _git(root, "branch", "-D", branch)
        raise
    stat = _git(path, "show", "--stat", "--format=%h %s", "HEAD").stdout.strip()
    return {"git_branch": branch, "worktree": str(path), "files": files, "stat": stat}
