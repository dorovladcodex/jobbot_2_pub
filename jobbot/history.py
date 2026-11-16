"""Vacancy history projected from durable search checkpoints."""
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.checkpoint.sqlite import SqliteSaver


def saved_runs(saver: SqliteSaver) -> list[dict]:
    # Read only the last state per run. Earlier checkpoints provide legacy dates.
    with saver.cursor(transaction=False) as cursor:
        rows = cursor.execute(
            "SELECT thread_id, MIN(checkpoint_id), MAX(checkpoint_id) "
            "FROM checkpoints WHERE checkpoint_ns = '' GROUP BY thread_id"
        ).fetchall()
    runs = []
    for run_id, first_id, last_id in rows:
        config = {"configurable": {"thread_id": run_id, "checkpoint_id": last_id}}
        checkpoint = saver.get_tuple(config).checkpoint
        state = checkpoint["channel_values"]
        if "shortlist" not in state:
            continue
        created_at = state.get("created_at")
        if not created_at:
            config["configurable"]["checkpoint_id"] = first_id
            created_at = saver.get_tuple(config).checkpoint["ts"]
        runs.append({"id": run_id, "created_at": created_at, "state": state})
    return sorted(runs, key=lambda run: (run["created_at"], run["id"]))


def vacancy_history(runs: list[dict], *, include_latest: bool = False) -> list[dict]:
    first_seen = {}
    for run in runs:
        for job in run["state"]["shortlist"]:
            first_seen.setdefault(job["id"], {
                **job, "first_seen_at": run["created_at"], "run_id": run["id"],
                "kept": job["id"] in run["state"].get("reviewed_ids", []),
            })
    latest_id = runs[-1]["id"] if runs else None
    jobs = [job for job in first_seen.values() if include_latest or job["run_id"] != latest_id]
    berlin = ZoneInfo("Europe/Berlin")
    return sorted(jobs, key=lambda job: (
        -datetime.fromisoformat(job["first_seen_at"]).astimezone(berlin).date().toordinal(),
        -(job.get("fit_score") if job.get("fit_score") is not None else job["score"] / 10),
        job["first_seen_at"], job["id"],
    ))
