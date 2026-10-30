"""Outils en lecture seule pour analyser un dépôt Git.

Aucune écriture, aucune commande shell libre : seules des commandes git
de consultation sont autorisées, et tout chemin reste confiné à la racine du dépôt.
"""
import fnmatch
import os
import re
import subprocess
from pathlib import Path

MAX_FILE_CHARS = 8000
MAX_RESULTS = 200

IGNORED_DIRS = {
    ".git", "vendor", "node_modules", "storage", "backups", "tmp",
    "public/build", ".venv", "__pycache__",
}
# Fichiers pouvant contenir des secrets : jamais lus ni cherchés.
SENSITIVE_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.sqlite", "*.xlsm", "*.xlsx"]


class ToolError(Exception):
    pass


def _is_sensitive(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, p) for p in SENSITIVE_PATTERNS) and not path.name.endswith(".example")


def _is_ignored(rel: Path) -> bool:
    rel_str = rel.as_posix()
    return any(rel_str == d or rel_str.startswith(d + "/") or d in rel.parts for d in IGNORED_DIRS)


class RepoTools:
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / ".git").exists():
            raise ValueError(f"{self.root} n'est pas un dépôt Git")

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError("Chemin hors du dépôt refusé.")
        return path

    def _walk(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(self.root)
            dirnames[:] = [d for d in dirnames if not _is_ignored(rel_dir / d)]
            for name in filenames:
                p = Path(dirpath) / name
                if not _is_sensitive(p):
                    yield p

    def list_files(self, directory: str = ".", pattern: str = "*") -> str:
        base = self._resolve(directory)
        if not base.is_dir():
            raise ToolError(f"Dossier introuvable : {directory}")
        found = [
            p.relative_to(self.root).as_posix()
            for p in self._walk(base)
            if fnmatch.fnmatch(p.name, pattern)
        ]
        found.sort()
        out = "\n".join(found[:MAX_RESULTS])
        if len(found) > MAX_RESULTS:
            out += f"\n... {len(found) - MAX_RESULTS} fichiers de plus (affine le dossier ou le motif)"
        return out or "Aucun fichier."

    def read_text(self, path: str) -> str:
        """Texte brut complet (sans numéros ni troncature), mêmes protections que read_file."""
        p = self._resolve(path)
        if _is_sensitive(p):
            raise ToolError("Fichier sensible : lecture refusée.")
        if not p.is_file():
            raise ToolError(f"Fichier introuvable : {path}")
        return p.read_text(encoding="utf-8", errors="replace")

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self._resolve(path)
        if _is_sensitive(p):
            raise ToolError("Fichier sensible : lecture refusée.")
        if not p.is_file():
            raise ToolError(f"Fichier introuvable : {path}")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(start_line, 1)
        chunk = lines[start - 1:end_line]
        text = "\n".join(f"{start + i}: {l}" for i, l in enumerate(chunk))
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n... [tronqué, demande une plage de lignes précise]"
        return text or "(fichier vide)"

    def search(self, query: str, directory: str = ".", glob: str = "*") -> str:
        base = self._resolve(directory)
        try:
            regex = re.compile(query)
        except re.error as exc:
            raise ToolError(f"Regex invalide : {exc}")
        hits = []
        for p in self._walk(base):
            if not fnmatch.fnmatch(p.name, glob):
                continue
            try:
                for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{p.relative_to(self.root).as_posix()}:{n}: {line.strip()[:200]}")
                        if len(hits) >= MAX_RESULTS:
                            return "\n".join(hits) + "\n... résultats tronqués"
            except OSError:
                continue
        return "\n".join(hits) or "Aucune correspondance."

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, timeout=30,
        )
        out = result.stdout if result.returncode == 0 else result.stderr
        return out[:MAX_FILE_CHARS] or "(aucune sortie)"

    def git_status(self) -> str:
        return self._git("status", "--short", "--branch")

    def git_log(self, count: int = 10, path: str | None = None) -> str:
        args = ["log", f"-{min(max(count, 1), 50)}", "--oneline", "--decorate"]
        if path:
            self._resolve(path)
            args += ["--", path]
        return self._git(*args)

    def git_diff(self, path: str | None = None, staged: bool = False) -> str:
        if path and _is_sensitive(self._resolve(path)):
            raise ToolError("Fichier sensible : diff refusé.")
        args = ["diff", "--no-color"]
        if staged:
            args.append("--staged")
        if path:
            args += ["--", path]
        else:
            args += ["--", ".", ":(exclude).env*", ":(exclude)*.pem", ":(exclude)*.key"]
        return self._git(*args)

    def search_docs(self, query: str) -> str:
        from agents import db

        hits = db.search_docs(query)
        if not hits:
            return "Aucun document correspondant."
        return "\n\n".join(f"[{h['title']}] ({h['source'] or 'saisie manuelle'})\n{h['content'][:1500]}" for h in hits)

    def propose_command(self, command: str, reason: str = "") -> str:
        from agents.commands import CommandRefused, store

        try:
            item = store.propose(self.root, command, reason)
        except CommandRefused as exc:
            raise ToolError(str(exc))
        return (f"Commande proposée (id {item.id}), EN ATTENTE de validation humaine. "
                "Elle n'est PAS exécutée. Indique à l'utilisateur de l'approuver via l'API, "
                "puis de te transmettre le résultat.")

    def call(self, name: str, arguments: dict) -> str:
        allowed = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search": self.search,
            "git_status": self.git_status,
            "git_log": self.git_log,
            "git_diff": self.git_diff,
            "propose_command": self.propose_command,
            "search_docs": self.search_docs,
        }
        if name not in allowed:
            return f"Outil inconnu : {name}"
        arguments = dict(arguments)
        for alias, real in (("line_start", "start_line"), ("line_end", "end_line"), ("file", "path")):
            if alias in arguments and real not in arguments:
                arguments[real] = arguments.pop(alias)
        for key in ("directory",):
            if arguments.get(key) == "":
                arguments[key] = "."
        if arguments.get("end_line") and arguments.get("start_line", 1) > arguments["end_line"]:
            return "Erreur : start_line doit être <= end_line."
        try:
            return allowed[name](**arguments)
        except ToolError as exc:
            return f"Erreur : {exc}"
        except TypeError as exc:
            return f"Arguments invalides : {exc}"


def _fn(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
        },
    }


TOOL_SCHEMAS = [
    _fn("list_files", "Liste les fichiers d'un dossier (récursif) du dépôt.",
        {"directory": {"type": "string"}, "pattern": {"type": "string", "description": "ex: *.php"}}),
    _fn("read_file", "Lit un fichier avec numéros de ligne.",
        {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    _fn("search", "Recherche une regex dans les fichiers du dépôt.",
        {"query": {"type": "string"}, "directory": {"type": "string"}, "glob": {"type": "string"}}, ["query"]),
    _fn("git_status", "Statut Git du dépôt."),
    _fn("git_log", "Derniers commits.", {"count": {"type": "integer"}, "path": {"type": "string"}}),
    _fn("git_diff", "Diff Git non commité.", {"path": {"type": "string"}, "staged": {"type": "boolean"}}),
    _fn("search_docs",
        "Cherche dans la base documentaire du projet (conventions, notes d'architecture, décisions).",
        {"query": {"type": "string"}}, ["query"]),
    _fn("propose_command",
        "Propose une commande (tests, lint, build) à l'utilisateur. Elle n'est exécutée qu'après son "
        "approbation explicite. Liste blanche : php artisan test, vendor/bin/phpunit, vendor/bin/phpstan analyse, "
        "npm run build|lint, npx tsc --noEmit, flutter analyze|test, git status|diff|log.",
        {"command": {"type": "string"}, "reason": {"type": "string"}}, ["command"]),
]
