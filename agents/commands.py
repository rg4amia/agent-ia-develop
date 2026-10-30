"""Exécution de commandes avec validation humaine explicite.

L'agent ne peut que PROPOSER une commande. Elle n'est exécutée que lorsque
l'utilisateur l'approuve via l'API (POST /commands/{id}/approve).
Pas de shell : la commande est découpée en arguments et doit correspondre à la liste blanche.
"""
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from agents import db

MAX_OUTPUT = 20000
EXEC_TIMEOUT = 300

# Préfixes autorisés (argv). Tout ce qui n'y correspond pas est refusé.
ALLOWED_PREFIXES = [
    ["php", "artisan", "test"],
    ["php", "artisan", "route:list"],
    ["php", "artisan", "about"],
    ["vendor/bin/phpunit"],
    ["vendor/bin/phpstan", "analyse"],
    ["vendor/bin/pint", "--test"],
    ["npm", "run", "build"],
    ["npm", "run", "lint"],
    ["npx", "tsc", "--noEmit"],
    ["flutter", "analyze"],
    ["flutter", "test"],
    ["git", "status"],
    ["git", "diff"],
    ["git", "log"],
]
SAFE_ARG = re.compile(r"^[\w./:=@,~^\-]+$")
# Options pouvant écrire ou exécuter autre chose.
FORBIDDEN_ARGS = {"--output", "-o", "--ext-diff", "--textconv", "--exec"}


class CommandRefused(Exception):
    pass


def validate(command: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise CommandRefused(f"Commande illisible : {exc}")
    if not argv:
        raise CommandRefused("Commande vide.")
    from agents.tools import _is_sensitive  # import tardif : évite l'import circulaire

    for arg in argv:
        if _is_sensitive(Path(arg)):
            raise CommandRefused(f"Fichier sensible refusé : {arg!r}")
        if not SAFE_ARG.match(arg):
            raise CommandRefused(f"Argument non autorisé : {arg!r} (pas de métacaractères shell).")
        if arg.split("=", 1)[0] in FORBIDDEN_ARGS or ".." in Path(arg).parts:
            raise CommandRefused(f"Argument interdit : {arg!r}")
    if not any(argv[:len(p)] == p for p in ALLOWED_PREFIXES):
        raise CommandRefused("Commande hors liste blanche.")
    return argv


@dataclass
class Proposal:
    id: str
    repo: str
    command: str
    reason: str
    status: str  # pending | rejected | executed
    created_at: str
    exit_code: int | None = None
    output: str | None = None


def _row(r) -> Proposal:
    return Proposal(**dict(r))


class CommandStore:
    def propose(self, repo: Path, command: str, reason: str = "") -> Proposal:
        validate(command)
        item = Proposal(id=uuid.uuid4().hex[:8], repo=str(repo), command=command, reason=reason,
                        status="pending", created_at=db.now())
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO proposals (id, repo, command, reason, status, created_at) VALUES (?,?,?,?,?,?)",
                (item.id, item.repo, item.command, item.reason, item.status, item.created_at),
            )
        return item

    def get(self, proposal_id: str) -> Proposal:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return _row(row)

    def list(self, status: str | None = None) -> list[Proposal]:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE (? IS NULL OR status = ?) ORDER BY created_at, rowid",
                (status, status),
            ).fetchall()
        return [_row(r) for r in rows]

    def _finish(self, item: Proposal) -> Proposal:
        with db.connect() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, exit_code = ?, output = ? WHERE id = ?",
                (item.status, item.exit_code, item.output, item.id),
            )
        return item

    def _claim(self, proposal_id: str, new_status: str) -> Proposal:
        """Passe pending → new_status de façon atomique : une proposition ne se traite qu'une fois."""
        with db.connect() as conn:
            done = conn.execute(
                "UPDATE proposals SET status = ? WHERE id = ? AND status = 'pending'", (new_status, proposal_id)
            ).rowcount
        item = self.get(proposal_id)
        if not done:
            raise CommandRefused(f"Déjà traitée ({item.status}).")
        return item

    def reject(self, proposal_id: str) -> Proposal:
        return self._claim(proposal_id, "rejected")

    def approve(self, proposal_id: str) -> Proposal:
        argv = validate(self.get(proposal_id).command)  # revalidation avant exécution
        item = self._claim(proposal_id, "executed")
        try:
            done = subprocess.run(
                argv, cwd=item.repo, capture_output=True, text=True, timeout=EXEC_TIMEOUT,
            )
            item.exit_code = done.returncode
            item.output = (done.stdout + done.stderr)[-MAX_OUTPUT:]
        except subprocess.TimeoutExpired:
            item.exit_code = -1
            item.output = f"Interrompu : délai de {EXEC_TIMEOUT}s dépassé."
        except FileNotFoundError as exc:
            item.exit_code = -1
            item.output = f"Programme introuvable : {exc}"
        return self._finish(item)


store = CommandStore()
