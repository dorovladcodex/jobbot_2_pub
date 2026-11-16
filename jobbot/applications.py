"""Durable application events backed by email evidence."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
import sqlite3
from urllib.parse import quote

from .catalog import canonical_url


STATUS_ORDER = {
    "applied": 1, "recruiter_reply": 2, "assessment": 3, "interview_invitation": 4,
    "interview_scheduled": 5, "offer": 6, "rejected": 7, "withdrawn": 8,
}
TERMINAL = {"offer", "rejected", "withdrawn"}


def normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def company_key(value: str) -> str:
    words = normalized(value).split()
    while len(words) > 1 and words[-1] in {"gmbh", "ag", "se", "ug", "kg", "co", "inc", "llc", "ltd", "limited", "corp"}:
        words.pop()
    return " ".join(words)


def title_key(value: str) -> str:
    value = re.sub(r"\b[wmfdx](?:\s*[/|]\s*[wmfdx]){1,3}\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:all genders|all gender|any gender)\b", " ", value, flags=re.I)
    return normalized(value)


def vacancy_url(value: str | None) -> str | None:
    try:
        return canonical_url(value) if value else None
    except ValueError:
        return None


def matching_jobs(event: dict, jobs: list[dict]) -> list[dict]:
    url = vacancy_url(event.get("job_url"))
    if url:
        exact = [job for job in jobs if vacancy_url(job["url"]) == url]
        if exact:
            return exact
    company, title = company_key(event["company"]), title_key(event["title"])
    return [job for job in jobs if company_key(job["company"]) == company
            and (not title or title_key(job["title"]) == title)]


def unique_match(application: dict, jobs: list[dict]) -> dict | None:
    candidates = matching_jobs(application, jobs)
    if len(candidates) != 1:
        return None
    job = candidates[0]
    url = vacancy_url(application.get("job_url"))
    if title_key(application["title"]) or (url is not None and url == vacancy_url(job["url"])):
        return job
    return None


class ApplicationStore:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
                job_id TEXT, job_url TEXT, match_state TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS application_job ON applications(mailbox, job_id)
                WHERE job_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS application_events (
                application_id TEXT NOT NULL, mailbox TEXT NOT NULL, message_id TEXT NOT NULL,
                thread_id TEXT NOT NULL, received_at TEXT NOT NULL, status TEXT NOT NULL,
                subject TEXT NOT NULL, evidence_quote TEXT NOT NULL, interview_at TEXT,
                PRIMARY KEY(application_id, mailbox, message_id)
            );
            CREATE TABLE IF NOT EXISTS processed_application_mail (
                mailbox TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(mailbox, message_id)
            );
            CREATE TABLE IF NOT EXISTS application_sync (
                mailbox TEXT PRIMARY KEY, synced_at TEXT NOT NULL, since_date TEXT NOT NULL, cursor TEXT NOT NULL
            );
        """)

    def cursor(self, mailbox: str) -> tuple[str, str]:
        row = self.conn.execute("SELECT * FROM application_sync WHERE mailbox = ?", (mailbox,)).fetchone()
        if row and row["cursor"]:
            return row["since_date"], row["cursor"]
        start = datetime.fromisoformat(row["synced_at"]) - timedelta(days=7) if row else (
            datetime.now(timezone.utc) - timedelta(days=180)
        )
        return start.date().isoformat(), ""

    def seen_messages(self, mailbox: str) -> set[str]:
        return {row[0] for row in self.conn.execute(
            "SELECT message_id FROM processed_application_mail WHERE mailbox = ?", (mailbox,),
        )}

    def reconcile(self, jobs: list[dict]) -> None:
        known_ids = {job["id"] for job in jobs}
        rows = self.conn.execute("SELECT * FROM applications").fetchall()
        for row in rows:
            if row["job_id"] in known_ids:
                continue
            job = unique_match(dict(row), jobs)
            if job:
                target = self.conn.execute(
                    "SELECT id FROM applications WHERE mailbox = ? AND job_id = ?", (row["mailbox"], job["id"]),
                ).fetchone()
                # A name-based backfill must not merge two existing applications.
                if target and target["id"] != row["id"]:
                    continue
                self.link(row["id"], job)

    def linked_job_ids(self, jobs: list[dict] | None = None) -> set[str]:
        if jobs is not None:
            self.reconcile(jobs)
        ids = set()
        for row in self.conn.execute("SELECT job_id, job_url FROM applications"):
            if row["job_id"]:
                ids.add(row["job_id"])
            if row["job_url"]:
                ids.add(sha256(canonical_url(row["job_url"]).encode()).hexdigest()[:20])
        return ids

    def close(self) -> None:
        self.conn.close()

    def save(self, mailbox: str, batch: dict, events: list[dict], jobs: list[dict], since: str) -> None:
        with self.conn:
            for event in sorted(events, key=lambda event: (event["received_at"], event["message_id"])):
                candidates = matching_jobs(event, jobs)
                title = title_key(event["title"])
                company = company_key(event["company"])
                thread_apps = self.conn.execute(
                    "SELECT DISTINCT a.* FROM applications a JOIN application_events e ON a.id = e.application_id "
                    "WHERE a.mailbox = ? AND e.thread_id = ?", (mailbox, event["thread_id"]),
                ).fetchall()
                matches = [row for row in thread_apps if company_key(row["company"]) == company
                           and (not title or not row["title"] or title_key(row["title"]) == title)
                           and (not event.get("job_url") or not row["job_url"]
                                or row["job_url"] == event["job_url"])]
                if not matches and event["title"] and len(candidates) <= 1:
                    matches = [row for row in self.conn.execute(
                        "SELECT * FROM applications WHERE mailbox = ?", (mailbox,),
                    ) if company_key(row["company"]) == company and title_key(row["title"]) == title
                        and (not event.get("job_url") or not row["job_url"] or row["job_url"] == event["job_url"])]
                exact_job = unique_match(event, jobs)
                if exact_job:
                    linked = self.conn.execute(
                        "SELECT * FROM applications WHERE mailbox = ? AND job_id = ?", (mailbox, exact_job["id"]),
                    ).fetchone()
                    if linked:
                        event_url = vacancy_url(event.get("job_url"))
                        if event_url and event_url != vacancy_url(exact_job["url"]):
                            exact_job = None
                        else:
                            matches = [linked]
                if len(matches) == 1:
                    application_id = matches[0]["id"]
                    if exact_job and not matches[0]["job_id"]:
                        self.conn.execute(
                            "UPDATE applications SET company = ?, title = ?, job_id = ?, job_url = ?, "
                            "match_state = 'matched' WHERE id = ?",
                            (event["company"], event["title"], exact_job["id"], exact_job["url"], application_id),
                        )
                else:
                    identity = json.dumps([mailbox, company, title, event.get("job_url"), event["thread_id"]])
                    application_id = sha256(identity.encode()).hexdigest()[:24]
                    match_state = "matched" if exact_job else (
                        "needs_review" if candidates or matches or not event["title"] else "unlinked"
                    )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (application_id, mailbox, event["company"], event["title"],
                         exact_job["id"] if exact_job else None,
                         exact_job["url"] if exact_job else event.get("job_url"), match_state),
                    )
                self.conn.execute(
                    "INSERT OR IGNORE INTO application_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (application_id, mailbox, event["message_id"], event["thread_id"], event["received_at"],
                     event["status"], event["subject"], event["evidence_quote"], event.get("interview_at")),
                )
            self.conn.executemany(
                "INSERT OR IGNORE INTO processed_application_mail VALUES (?, ?)",
                [(mailbox, message["message_id"]) for message in batch["messages"]],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO application_sync VALUES (?, ?, ?, ?)",
                (mailbox, datetime.now(timezone.utc).isoformat(), since, batch["next_page_token"]),
            )

    def list(self) -> dict:
        applications = []
        for row in self.conn.execute("SELECT * FROM applications"):
            application = dict(row)
            events = [dict(event) for event in self.conn.execute(
                "SELECT * FROM application_events WHERE application_id = ? ORDER BY received_at, message_id",
                (row["id"],),
            )]
            if not events:
                continue
            terminal = [event for event in events if event["status"] in TERMINAL]
            current = terminal[-1] if terminal else max(
                events, key=lambda event: (STATUS_ORDER[event["status"]], event["received_at"]),
            )
            for event in events:
                event["email_url"] = (
                    "https://mail.google.com/mail/u/?authuser=" + quote(row["mailbox"], safe="")
                    + "#all/" + event["message_id"]
                )
            application.update(status=current["status"], updated_at=events[-1]["received_at"],
                               first_seen_at=events[0]["received_at"], interview_at=current["interview_at"],
                               events=events)
            applications.append(application)
        syncs = [dict(row) for row in self.conn.execute(
            "SELECT mailbox, synced_at, cursor != '' AS has_more FROM application_sync ORDER BY mailbox",
        )]
        return {"applications": sorted(applications, key=lambda item: item["updated_at"], reverse=True),
                "mailboxes": syncs}

    def link(self, application_id: str, job: dict) -> None:
        with self.conn:
            row = self.conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            if row is None:
                raise ValueError("Application not found")
            target = self.conn.execute(
                "SELECT id FROM applications WHERE mailbox = ? AND job_id = ?", (row["mailbox"], job["id"]),
            ).fetchone()
            if target and target["id"] != application_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO application_events "
                    "SELECT ?, mailbox, message_id, thread_id, received_at, status, subject, evidence_quote, interview_at "
                    "FROM application_events WHERE application_id = ?", (target["id"], application_id),
                )
                self.conn.execute("DELETE FROM application_events WHERE application_id = ?", (application_id,))
                self.conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))
            else:
                self.conn.execute(
                    "UPDATE applications SET job_id = ?, job_url = ?, match_state = 'matched' WHERE id = ?",
                    (job["id"], job["url"], application_id),
                )
