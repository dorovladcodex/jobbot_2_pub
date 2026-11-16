# Running JobBot

## Live workspace

Follow the installation commands in the [README](../README.md), then verify your existing Codex CLI sign-in with `codex login status`. Upload your own PDF or text CV and review the suggested skills and roles before searching.

Select the sources you want. Codex web passes cover general boards, company career pages, remote roles and specialist sources. Public feeds include Arbeitnow, GermanTechJobs, RemoteOK and Remotive. Optional direct Greenhouse and Lever boards are configured through an environment variable. Source availability is reported per run; it is never guaranteed.

On Windows, JobBot prefers the CLI bundled with the desktop app, then falls back to the CLI on PATH. `JOBBOT_CODEX_BIN` overrides that choice. A stale CLI may fail to read a newer desktop model cache.

For frontend development, run `npm --prefix frontend run dev` alongside the API. Vite proxies `/api` to port 8000. For normal use, build the frontend and let FastAPI serve it.

## Offline walkthrough

No model or mailbox connection is needed for this route. In PowerShell, set:

```powershell
$env:JOBBOT_SEARCH_MODE = 'demo'
$env:JOBBOT_PROFILE = 'baseline'
$env:JOBBOT_MATCHER = 'baseline'
$env:JOBBOT_DB = 'runtime/demo.sqlite'
python -m uvicorn jobbot.api:app --host 127.0.0.1 --port 8000
```

On bash, use `export NAME=value` for the same four settings before starting the API.

Upload [`examples/candidate.txt`](../examples/candidate.txt), review the detected skills, and run a search. The source is a fictional catalog. Baseline mode shows skill coverage; overall fit scores remain unassessed. Keep a vacancy and confirm the review. Run another search to see previously shown vacancies move to Historical. Gmail sync is optional and is not part of this offline route.

The UI intentionally has no “Load demo CV” button; offline input uses the normal upload flow.

## Useful settings

Set environment variables in the shell that starts the server. The app does not automatically load a `.env` file.

| Variable | Default / use |
| --- | --- |
| `JOBBOT_SEARCH_MODE` | `live`; use `demo` for synthetic sources. |
| `JOBBOT_PROFILE` | `codex`; use `baseline` for local skill detection. |
| `JOBBOT_MATCHER` | `codex` in live mode, `baseline` in demo mode. |
| `JOBBOT_DB` | `runtime/jobbot.sqlite`; use a separate path for demos. |
| `JOBBOT_CODEX_BIN` | Optional native Codex executable path. |
| `JOBBOT_ATS_BOARDS` | Optional comma-separated boards, e.g. `greenhouse:example,lever:example`. |
| `JOBBOT_GMAIL_APP_ID` | Optional installed Gmail registration override. |
| `JOBBOT_GMAIL_MODEL` | Optional model available to your Codex account for mailbox reading. |

## Gmail sync

Connect Gmail in the same Codex environment that starts JobBot. Enter that connected address in **Application inbox**, then choose **Sync email**. Entering an address does not grant access to an arbitrary mailbox.

If another page exists, **Continue sync** processes it. Run until no more pages remain to finish the initial backfill. Applications appear even when their vacancies were never found by a search.

On failure, inspect `runtime/gmail-sync-last-error.json` (or the equivalent next to a custom database). It records the stage, executable and filtered tool-call metadata. It excludes normal tool results and mail bodies. Do not publish runtime diagnostics or your database.

## Publishing the code versus hosting the service

A public GitHub repository distributes the code; it does not run the Python API or the user's authenticated Codex/Gmail processes. GitHub Pages cannot host this backend.

Keep this version bound to `127.0.0.1`. Before hosting it for other users, add authentication, per-user storage and authorization, job execution outside request handlers, request limits, HTTPS and explicit retention/deletion controls. Do not upload a real mailbox database as a demo dataset.
