"""Déclencheurs de workflows : manuel, intervalle, quotidien, webhook.

Un déclencheur automatique ne contourne aucune approbation : les nœuds commande et patch
attendent toujours une validation humaine (30 min max), même lancés par le planificateur.
"""
import secrets
import threading
import time
from datetime import datetime
from typing import Callable

from agents import db

KINDS = {"manual", "interval", "daily", "webhook"}
MIN_INTERVAL = 5
MAX_INTERVAL = 10080  # une semaine
POLL_SECONDS = 15


class TriggerError(Exception):
    pass


def _default(workflow_id: str) -> dict:
    return {"workflow_id": workflow_id, "kind": "manual", "every_minutes": None, "daily_at": None,
            "enabled": False, "webhook_token": None, "input": "", "last_run_at": None}


def get(workflow_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM triggers WHERE workflow_id = ?", (workflow_id,)).fetchone()
    if not row:
        return _default(workflow_id)
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    return out


def save(workflow_id: str, kind: str, every_minutes: int | None, daily_at: str | None,
         enabled: bool, input_text: str) -> dict:
    if kind not in KINDS:
        raise TriggerError(f"Type de déclencheur inconnu : {kind}")
    if kind == "interval":
        if every_minutes is None or not MIN_INTERVAL <= every_minutes <= MAX_INTERVAL:
            raise TriggerError(f"Intervalle entre {MIN_INTERVAL} et {MAX_INTERVAL} minutes.")
    if kind == "daily":
        try:
            datetime.strptime(daily_at or "", "%H:%M")
        except ValueError:
            raise TriggerError("Heure invalide (format HH:MM).")
    previous = get(workflow_id)
    token = previous["webhook_token"]
    if kind == "webhook" and not token:
        token = secrets.token_urlsafe(24)
    # À l'activation, on part de maintenant : pas de lancement immédiat pour une heure déjà passée.
    last = previous["last_run_at"]
    if enabled and (not previous["enabled"] or previous["kind"] != kind or last is None):
        last = db.now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO triggers (workflow_id, kind, every_minutes, daily_at, enabled, webhook_token, input, last_run_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET kind=excluded.kind, "
            "every_minutes=excluded.every_minutes, daily_at=excluded.daily_at, enabled=excluded.enabled, "
            "webhook_token=excluded.webhook_token, input=excluded.input, last_run_at=excluded.last_run_at",
            (workflow_id, kind, every_minutes if kind == "interval" else None,
             daily_at if kind == "daily" else None, int(enabled), token, input_text, last),
        )
    return get(workflow_id)


def rotate_token(workflow_id: str) -> dict:
    current = get(workflow_id)
    if current["kind"] != "webhook":
        raise TriggerError("Le déclencheur n'est pas de type webhook.")
    with db.connect() as conn:
        conn.execute("UPDATE triggers SET webhook_token = ? WHERE workflow_id = ?",
                     (secrets.token_urlsafe(24), workflow_id))
    return get(workflow_id)


def workflow_for_token(token: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT workflow_id, webhook_token FROM triggers WHERE kind = 'webhook' AND enabled = 1 AND webhook_token = ?",
            (token,),
        ).fetchone()
    if row and secrets.compare_digest(row["webhook_token"], token):
        return row["workflow_id"]
    return None


def touch(workflow_id: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE triggers SET last_run_at = ? WHERE workflow_id = ?", (db.now(), workflow_id))


def is_due(t: dict, now: datetime) -> bool:
    last = datetime.fromisoformat(t["last_run_at"]) if t["last_run_at"] else None
    if t["kind"] == "interval":
        return last is None or (now - last).total_seconds() >= t["every_minutes"] * 60
    if t["kind"] == "daily":
        hh, mm = map(int, t["daily_at"].split(":"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return now >= slot and (last is None or last < slot)
    return False


def due_workflows(now: datetime) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE enabled = 1 AND kind IN ('interval', 'daily')"
        ).fetchall()
    return [dict(r) for r in rows if is_due(dict(r), now)]


_started = False


def start_scheduler(fire: Callable[[str, str], None]) -> None:
    """Lance la boucle de planification (une seule fois par processus)."""
    global _started
    if _started:
        return
    _started = True

    def loop() -> None:
        while True:
            try:
                for t in due_workflows(datetime.now()):
                    touch(t["workflow_id"])  # avant le tir : pas de rafale d'échecs
                    try:
                        fire(t["workflow_id"], t["input"])
                    except Exception:
                        pass  # workflow supprimé, dépôt absent, exécution déjà active…
            except Exception:
                pass
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="scheduler").start()
