import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Evidence = { skill: string; cv_quote: string; vacancy_quote: string };
type Job = {
  id: string; company: string; title: string; url: string; mode: string; location: string;
  score: number; scorer: string; sources: string[]; unknown: string[]; evidence: Evidence[];
  fit_score?: number | null; salary?: string | null; language?: string | null;
  language_risk?: "low" | "medium" | "high" | null; language_risk_reason?: string;
  contract_type?: string | null; analysis?: string; not_evidenced_skills?: string[];
  recommendation?: string | null; content_status?: string;
};
type Run = {
  id: string; status: "awaiting_review" | "reviewed" | "empty" | "sources_unavailable";
  catalog_count: number; shortlist: Job[]; unavailable_sources: string[]; reviewed_ids: string[];
  source_results?: { id: string; status: "ok" | "empty" | "unavailable"; count: number }[];
  excluded: { id: string; title: string; reasons: string[] }[];
  created_at?: string; historical_count?: number;
};
type HistoricalJob = Job & { first_seen_at: string; run_id: string; kept: boolean };
type History = { jobs: HistoricalJob[]; latest_run: Run | null };
type ApplicationEvent = {
  status: string; received_at: string; subject: string; evidence_quote: string; email_url: string;
};
type Application = {
  id: string; mailbox: string; company: string; title: string; job_url: string | null;
  status: string; match_state: string; updated_at: string; first_seen_at: string; interview_at: string | null;
  events: ApplicationEvent[]; vacancy: Job | null;
};
type Applications = {
  applications: Application[];
  mailboxes: { mailbox: string; synced_at: string; has_more: boolean }[]; plugin_installed: boolean;
};
type SyncResult = Applications & { scanned: number; updates: number; has_more: boolean };
const APPLICATION_STATUS: Record<string, string> = {
  applied: "Applied", recruiter_reply: "Recruiter reply", assessment: "Assessment",
  interview_invitation: "Interview invitation", interview_scheduled: "Interview scheduled",
  rejected: "Rejected", offer: "Offer", withdrawn: "Withdrawn",
};
const interviewDate = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Berlin",
});
const historyDate = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium", timeZone: "Europe/Berlin",
});
type ResumeProfile = {
  cv: string; skills: string[]; target_roles?: string[]; method?: "codex" | "baseline" | "fallback";
};
type Demo = Pick<ResumeProfile, "cv" | "skills" | "target_roles"> & {
  matcher: string; catalog: string; profile_uses_codex: boolean;
  search_sources?: {
    id: string; label: string; kind?: "web" | "ats" | "feed" | "demo"; default_selected?: boolean; domains?: string[];
  }[];
};
const WORK_MODES = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" } as const;
const SOURCE_GROUPS = {
  web: "Codex web search",
  feed: "Public feeds",
  ats: "Direct ATS boards (optional)",
  demo: "Demo catalog",
} as const;
type SourceKind = keyof typeof SOURCE_GROUPS;
type WorkMode = keyof typeof WORK_MODES;

function commaList(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "Request failed (" + response.status + ")");
  }
  return response.json();
}

function post(body: object): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function LanguageRisk({ job }: { job: Job }) {
  const level = { low: 1, medium: 2, high: 3 }[job.language_risk ?? "low"];
  const label = job.language_risk
    ? job.language_risk[0].toUpperCase() + job.language_risk.slice(1) : "Unknown";
  return <span className={"language-risk risk-" + (job.language_risk ?? "unknown")}
    title={job.language_risk_reason} aria-label={"Language risk " + label}>
    <span className="risk-meter" aria-hidden="true">
      {[1, 2, 3].map((bar) => <i key={bar}
        className={job.language_risk && bar <= level ? "filled" : ""} />)}
    </span>
    <span>{label}</span>
  </span>;
}

function ApplicationCard({ application }: { application: Application }) {
  return <article className={"job application-card application-" + application.status}>
    <div className="job-heading">
      <div className="job-title">
        <h3>{application.company} <span className="muted">—</span> {application.title || "Role not stated"}</h3>
        <p className="muted small">{application.mailbox} · Updated {historyDate.format(new Date(application.updated_at))}</p>
      </div>
      <span className={"application-status status-" + application.status}>
        {APPLICATION_STATUS[application.status] ?? application.status}
      </span>
    </div>
    {application.vacancy && <p className="job-location">
      {application.vacancy.location || "Location not specified"}
      <span className="work-mode">{WORK_MODES[application.vacancy.mode as WorkMode] ?? "Work mode not specified"}</span>
      {application.vacancy.fit_score != null && <span>Fit {application.vacancy.fit_score.toFixed(1)}/10</span>}
    </p>}
    {application.interview_at && <p className="interview-time">
      Interview · {interviewDate.format(new Date(application.interview_at))} · Europe/Berlin
    </p>}
    <details className="job-details">
      <summary>Email timeline <span className="muted">{application.events.length} updates</span></summary>
      {application.events.map((event, index) => <div className="application-event" key={event.email_url + index}>
        <p><b>{APPLICATION_STATUS[event.status] ?? event.status}</b>
          <span className="muted"> · {historyDate.format(new Date(event.received_at))}</span></p>
        <p className="muted small">{event.subject}</p>
        <blockquote>{event.evidence_quote}</blockquote>
        <a href={event.email_url} target="_blank" rel="noreferrer">Open source email ↗</a>
      </div>)}
    </details>
    <div className="job-footer">
      <a href={application.events[application.events.length - 1].email_url} target="_blank" rel="noreferrer">
        Latest email ↗
      </a>
      {application.job_url && <a href={application.job_url} target="_blank" rel="noreferrer">Open vacancy ↗</a>}
    </div>
  </article>;
}

function App() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("jobbot-theme");
    return saved === "light" || saved === "dark" ? saved : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("jobbot-theme", theme);
  }, [theme]);
  const [demo, setDemo] = useState<Demo>();
  const [cv, setCv] = useState("");
  const [resumeName, setResumeName] = useState("");
  const [profileMethod, setProfileMethod] = useState<ResumeProfile["method"]>();
  const [skills, setSkills] = useState("");
  const [location, setLocation] = useState("");
  const [workModes, setWorkModes] = useState<WorkMode[]>([]);
  const [localAreas, setLocalAreas] = useState("");
  const [roles, setRoles] = useState("");
  const [language, setLanguage] = useState("any");
  const [german, setGerman] = useState("");
  const [contract, setContract] = useState("any");
  const [seniority, setSeniority] = useState("any");
  const [salary, setSalary] = useState("");
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  const [run, setRun] = useState<Run>();
  const [history, setHistory] = useState<HistoricalJob[]>([]);
  const [activeTab, setActiveTab] = useState<"latest" | "historical" | "applications">("latest");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [applications, setApplications] = useState<Applications>();
  const [applicationEmail, setApplicationEmail] = useState(() => localStorage.getItem("jobbot-application-email") ?? "");
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncNotice, setSyncNotice] = useState("");
  const [page, setPage] = useState(1);
  const [wideLayout, setWideLayout] = useState(() => window.matchMedia("(min-width: 1500px)").matches);
  const [pageSizeChoice, setPageSizeChoice] = useState<number | null>(null);
  const pageSize = pageSizeChoice ?? (wideLayout ? 10 : 5);
  useEffect(() => {
    const query = window.matchMedia("(min-width: 1500px)");
    const update = () => setWideLayout(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  const [sort, setSort] = useState("default");
  const reviewPending = run?.status === "awaiting_review";
  const sources = demo?.search_sources ?? [];
  const sourceLabels = new Map(sources.map((source) => [source.id, source.label]));
  const allJobs = activeTab === "applications" ? [] : activeTab === "historical" ? history : run?.shortlist ?? [];
  const orderedJobs = sort === "fit" ? [...allJobs].sort((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1)) : allJobs;
  const total = activeTab === "applications" ? applications?.applications.length ?? 0 : orderedJobs.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(page, pageCount);
  const startIndex = (currentPage - 1) * pageSize;
  const displayedJobs = orderedJobs.slice(startIndex, startIndex + pageSize);
  const displayedApplications = applications?.applications.slice(startIndex, startIndex + pageSize) ?? [];
  useEffect(() => { setPage(1); }, [activeTab, pageSize, sort, run?.id, total]);
  const mailboxState = applications?.mailboxes.find((mailbox) => mailbox.mailbox === applicationEmail.trim().toLowerCase());

  useEffect(() => {
    request<Demo>("/api/demo").then((data) => {
      setDemo(data);
      setSourceIds(data.search_sources?.filter((source) => source.default_selected !== false)
        .map((source) => source.id) ?? []);
    }).catch((e: Error) => setError(e.message));
    request<History>("/api/history").then((data) => {
      setHistory(data.jobs);
      if (data.latest_run) {
        setRun(data.latest_run);
        setSelected(data.latest_run.reviewed_ids);
      }
    }).catch((e: Error) => setError(e.message)).finally(() => setHistoryLoading(false));
    request<Applications>("/api/applications").then(setApplications).catch((e: Error) => setError(e.message));
  }, []);

  async function action(label: string, work: () => Promise<void>) {
    setBusy(label);
    setError("");
    try { await work(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Unexpected error"); }
    finally { setBusy(""); }
  }

  function search(event: FormEvent) {
    event.preventDefault();
    if (!sourceIds.length) return;
    void action("Searching", async () => {
      const data = await request<Run>("/api/runs", post({
        cv, skills: commaList(skills), location, work_modes: workModes,
        local_areas: commaList(localAreas), target_roles: commaList(roles), language,
        max_german_level: german || null, contract_type: contract, seniority,
        min_salary_eur: salary ? Number(salary) : null, source_ids: sourceIds,
      }));
      setRun(data);
      setSelected([]);
      setActiveTab("latest");
      const saved = await request<History>("/api/history");
      setHistory(saved.jobs);
      setApplications(await request<Applications>("/api/applications"));
    });
  }

  async function refreshAfterSync(data: Applications) {
    setApplications(data);
    const saved = await request<History>("/api/history");
    setHistory(saved.jobs);
    if (saved.latest_run) {
      setRun(saved.latest_run);
      const ids = new Set(saved.latest_run.shortlist.map((job) => job.id));
      setSelected((previous) => previous.filter((id) => ids.has(id)));
    }
  }

  async function syncEmail(event: FormEvent) {
    event.preventDefault();
    setSyncBusy(true); setError(""); setSyncNotice("");
    try {
      const email = applicationEmail.trim().toLowerCase();
      const data = await request<SyncResult>("/api/applications/sync", post({ email }));
      localStorage.setItem("jobbot-application-email", email);
      await refreshAfterSync(data);
      setActiveTab("applications");
      setSyncNotice(data.scanned + " emails checked · " + data.updates + " application updates recorded."
        + (data.has_more ? " More messages are available. Continue sync to load the next page." : ""));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Email sync failed");
    } finally { setSyncBusy(false); }
  }

  return <main>
    <header className="workspace-header">
      <div className="brand"><svg className="brand-symbol" viewBox="0 0 44 44" aria-hidden="true"><rect width="44" height="44" rx="14" fill="currentColor"/><path d="M13 29L29 13M16 13h13v13" fill="none" stroke="#d7ff91" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"/><circle cx="13" cy="29" r="3" fill="#fff"/></svg><span className="brand-name">jobbot<span className="brand-dot">.</span></span><span className="brand-version">2.0</span></div>
      <div className="header-caption">Your career, thoughtfully organized.</div>
      <button type="button" className="theme-toggle secondary" aria-label={theme === "light" ? "Switch to dark theme" : "Switch to light theme"}
        aria-pressed={theme === "dark"} onClick={() => setTheme(theme === "light" ? "dark" : "light")}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
          {theme === "light" ? <path d="M20.2 15.2A8.6 8.6 0 018.8 3.8 8.6 8.6 0 1020.2 15.2Z" /> : <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></>}
        </svg><span>{theme === "light" ? "Dark mode" : "Light mode"}</span>
      </button>

    </header>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="workspace">
      <form className="panel profile-panel" onSubmit={search}>
        <h2>Your profile & search</h2>
        <fieldset disabled={Boolean(busy)}>
          <div className="actions cv-actions">

            <label className="upload">{resumeName ? "Replace CV" : "Upload CV"} <input type="file" accept=".pdf,.txt" onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void action("Analyzing CV", async () => {
                const body = new FormData(); body.append("file", file);
                const profile = await request<ResumeProfile>("/api/resume", { method: "POST", body });
                const suggestedRoles = profile.target_roles?.join(", ") ?? "";
                setCv(profile.cv);
                setResumeName(file.name);
                setSkills(profile.skills.join(", "));
                setRoles(suggestedRoles);
                setProfileMethod(profile.method);
              });
            }} /></label>
          </div>
          <p className="muted small resume-status" role="status">{busy === "Analyzing CV" ? "Analyzing CV…" : resumeName
            ? resumeName + (profileMethod === "codex"
              ? " loaded. Review the skills and roles suggested by Codex."
              : " loaded. Review the detected skills and enter target roles.")
            : demo?.profile_uses_codex
              ? "Codex analyzes your CV after email and phone masking."
              : "Upload a PDF or text CV to populate skills."}</p>
          {profileMethod === "fallback" && <p className="warning">Codex profile analysis unavailable. Using local skill detection.</p>}
          <label>Skills to match<input value={skills} onChange={(event) => setSkills(event.target.value)} required placeholder="Python, SQL, Azure, Databricks" /></label>
          <label>Target roles<input value={roles} onChange={(event) => setRoles(event.target.value)} placeholder="AI Engineer, Data Engineer" /></label>
          <label>Country / base location<input value={location} onChange={(event) => setLocation(event.target.value)} maxLength={100} placeholder="Germany" /></label>
          <label>Hybrid and on-site areas<input value={localAreas} onChange={(event) => setLocalAreas(event.target.value)} placeholder="Dortmund, Bochum, NRW" /></label>
          <fieldset className="mode-options">
            <legend>Work modes <span className="muted small">(none selected = any)</span></legend>
            {(Object.keys(WORK_MODES) as WorkMode[]).map((option) => <label key={option}>
              <input type="checkbox" checked={workModes.includes(option)}
                onChange={(event) => setWorkModes(event.target.checked ? [...workModes, option] : workModes.filter((item) => item !== option))} />
              {WORK_MODES[option]}</label>)}
          </fieldset>
          <div className="filter-grid">
            <label>Working language<select value={language} onChange={(event) => setLanguage(event.target.value)}>
              <option value="any">Any</option><option value="english">Prefer English</option>
            </select></label>
            <label>Max. required German<select value={german} onChange={(event) => setGerman(event.target.value)}>
              <option value="">Any</option>{["A1", "A2", "B1", "B2", "C1", "C2"].map((level) => <option key={level}>{level}</option>)}
            </select></label>
            <label>Contract<select value={contract} onChange={(event) => setContract(event.target.value)}>
              <option value="any">Any</option><option value="permanent">Permanent</option>
              <option value="fixed_term">Fixed-term</option><option value="freelance">Freelance / B2B</option>
            </select></label>
            <label>Seniority<select value={seniority} onChange={(event) => setSeniority(event.target.value)}>
              <option value="any">Any</option><option value="senior_plus">Senior and above</option>
            </select></label>
          </div>
          <label>Minimum gross salary (EUR / year)<input type="number" min={1} max={2000000} step={1}
            value={salary} onChange={(event) => setSalary(event.target.value)} placeholder="85000" /></label>
          <p className="muted small filter-note">Unverified requirements stay visible for review.</p>
          <details className="source-picker"><summary>Search sources <span>{sourceIds.length} selected</span></summary><fieldset className="search-sources">
            <legend>Search sources</legend>
            {(Object.keys(SOURCE_GROUPS) as SourceKind[]).map((kind) => {
              const group = sources.filter((source) => (source.kind ?? "ats") === kind);
              return group.length > 0 && <fieldset key={kind} className="mode-options">
                <legend className="muted">{SOURCE_GROUPS[kind]}</legend>
                {group.map((source) => <label key={source.id}>
                  <input type="checkbox" checked={sourceIds.includes(source.id)} onChange={(event) => {
                    const checked = event.target.checked;
                    setSourceIds((ids) => checked ? [...ids, source.id] : ids.filter((id) => id !== source.id));
                  }} />
                  <span>{source.label}
                    {source.domains && source.domains.length > 1 && <small className="source-sites muted">
                      {source.domains.join(", ")}
                    </small>}
                  </span>
                </label>)}
              </fieldset>;
            })}
          </fieldset>
          </details>
          {demo && !demo.search_sources && <p className="warning filter-note">Restart the JobBot server and reload this page to use source selection.</p>}
          {demo?.search_sources && !sourceIds.length && <p className="muted small filter-note">Select at least one search source.</p>}
          {busy === "Searching" && <p className="muted small filter-note" role="status">Selected sources are running. Web search can take a few minutes.</p>}
          <button type="submit" disabled={!cv || !sourceIds.length || historyLoading}>{busy || "Run search"}</button>
        </fieldset>
      </form>
      <section className="panel results" aria-busy={Boolean(busy)}>
        <div className="results-heading"><div><h1>Find your next role.</h1>
          <p className="muted">A thoughtful shortlist, grounded in your experience.</p></div>
                  <form className="email-sync" onSubmit={(event) => void syncEmail(event)}>
          <label className="inbox-label" htmlFor="application-email">Application inbox</label>
          <div className="inbox-controls">
            <div className="inbox-input-wrap">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6"/></svg>
              <input id="application-email" type="email" value={applicationEmail} required maxLength={254}
                disabled={syncBusy} autoComplete="email" placeholder="you@example.com"
                onChange={(event) => setApplicationEmail(event.target.value)} />
            </div>
            <button type="submit" disabled={syncBusy || !applications}>
              {syncBusy ? "Syncing…" : mailboxState?.has_more ? "Continue sync" : "Sync email"}
            </button>
          </div>
          <p className="inbox-last-sync">{mailboxState
            ? "Last sync · " + interviewDate.format(new Date(mailboxState.synced_at)) + (mailboxState.has_more ? " · More available" : "")
            : "Not synced yet"}</p>
          {applications && !applications.plugin_installed && <p className="warning">Connect the Gmail plugin in Codex to sync.</p>}
          {syncNotice && <p className="sync-notice" role="status">{syncNotice}</p>}
        </form>
        </div>
        <div className="results-toolbar">
        <div className="result-tabs" role="group" aria-label="Vacancy view">
          <button type="button" className={activeTab === "latest" ? "" : "secondary"}
            aria-pressed={activeTab === "latest"} onClick={() => setActiveTab("latest")}>Latest results ({run?.shortlist.length ?? 0})</button>
          <button type="button" className={activeTab === "historical" ? "" : "secondary"}
            aria-pressed={activeTab === "historical"} onClick={() => setActiveTab("historical")}>
            Historical ({history.length})
          </button>
          <button type="button" className={activeTab === "applications" ? "" : "secondary"}
            aria-pressed={activeTab === "applications"} onClick={() => setActiveTab("applications")}>
            Applications ({applications?.applications.length ?? 0})
          </button>
        </div>
        <label className="sort-control">Sort by <select value={sort} onChange={(event) => setSort(event.target.value)}>
          <option value="default">{activeTab === "historical" ? "Date & relevance" : activeTab === "applications" ? "Latest update" : "Search order"}</option>
          {activeTab !== "applications" && <option value="fit">Best fit</option>}
        </select></label>
        </div>
        <div className="results-body" key={activeTab + currentPage + pageSize + sort}>
        {activeTab === "applications" && <>
          {!applications && <p className="muted" role="status">Loading applications.</p>}
          {applications?.applications.length === 0 && <p className="muted">
            Enter your connected email and select Sync email to find application confirmations and updates.
          </p>}
          <div className="vacancy-grid">
          {displayedApplications.map((application) => <ApplicationCard key={application.id}
            application={application} />)}
          </div>
        </>}
        {historyLoading && <p className="muted" role="status">Loading saved vacancies.</p>}
        {activeTab === "latest" && !run && !historyLoading && <p className="muted">Upload your CV to find your next opportunity.</p>}
        {activeTab === "historical" && <p className="muted small">
          Previous results, newest date first and highest relevance within each date. Dates use Europe/Berlin.
        </p>}
        {activeTab === "historical" && !history.length && !historyLoading && <p className="muted">
          No historical vacancies yet. Results move here after the next search.
        </p>}
        {activeTab === "latest" && run && <>
          <p className="muted small">{run.shortlist.length} new matches · {run.catalog_count} found · {run.excluded.length} excluded</p>
          {run.unavailable_sources.length > 0 && <p className="warning">
            Unavailable sources · {run.unavailable_sources.map((id) => sourceLabels.get(id) ?? id).join(", ")}
          </p>}
          {Boolean(run.source_results?.length) && <details className="small">
            <summary>Source coverage</summary>
            {run.source_results?.map(({ id, status, count }) => <p key={id}>
              {sourceLabels.get(id) ?? id} · {status === "unavailable" ? "Unavailable" : count + " postings"}
            </p>)}
          </details>}
          {Boolean(run.historical_count) && <p className="muted small">
            {run.historical_count} previously shown vacancies moved to Historical.
          </p>}
          {!run.shortlist.length && <p className="muted">
            {run.status === "sources_unavailable" ? "Selected sources are unavailable." : "No new matching vacancies in this search."}
          </p>}
        </>}
          <div className="vacancy-grid">
          {displayedJobs.map((job) => <article className="job" key={job.id}>
            <div className="job-heading">
              <div className="job-title">
                <span className="company-mark" aria-hidden="true">{job.company.slice(0, 2).toUpperCase()}</span><h3>{job.company} <span className="muted">—</span> {job.title}</h3>
                <p className="job-location">{job.location || "Location not specified"}
                  <span className="work-mode">{WORK_MODES[job.mode as WorkMode] ?? "Work mode not specified"}</span>
                </p>
              </div>
              <div className="fit-score" title={job.fit_score == null
                ? "Overall model assessment is unavailable. Skill coverage remains in the details."
                : "Overall model assessment of the role, experience and search preferences"}>
                <span>Fit score</span>
                <strong>{job.fit_score == null ? "—" : job.fit_score.toFixed(1)}
                  {job.fit_score != null && <small>/10</small>}</strong>
              </div>
            </div>
            <dl className="job-facts">
              <div><dt>Salary</dt><dd>{job.salary || "Not specified"}</dd></div>
              <div><dt>Language</dt><dd>{job.language || "Not specified"}</dd></div>
              <div><dt>Language risk</dt><dd><LanguageRisk job={job} /></dd></div>
              <div className="job-source"><dt>Source</dt>
                <dd>{job.sources.map((id) => sourceLabels.get(id) ?? id).join(", ")}</dd>
              </div>
            </dl>
            <details className="job-details">
              <summary>Analysis & details <span className="muted">{job.evidence.length} tech matches</span></summary>
              <div className="analysis-body">
                <h4>Analysis</h4>
                <p>{job.analysis || job.evidence.length + " selected skills have supporting quotes in the CV and posting."}</p>
                {job.recommendation && <><h4>Recommendation</h4><p>{job.recommendation}</p></>}
                <h4>Core tech match</h4>
                {job.evidence.length > 0 ? <div className="tech-tags">
                  {job.evidence.map((item) => <span key={item.skill}>{item.skill}</span>)}
                </div> : <p className="muted">No supported matches in the available text.</p>}
                {Boolean(job.not_evidenced_skills?.length) && <p className="muted small">
                  Not yet evidenced · {job.not_evidenced_skills?.join(", ")}
                </p>}
                <dl className="detail-facts">
                  <div><dt>Contract type</dt><dd>{job.contract_type || "Not specified"}</dd></div>
                  <div><dt>Language risk</dt><dd>{job.language_risk_reason || "Language requirements need verification."}</dd></div>
                  {"first_seen_at" in job && <div><dt>First shown</dt>
                    <dd>{historyDate.format(new Date(String(job.first_seen_at)))}</dd>
                  </div>}
                </dl>
                {job.unknown.length > 0 && <div className="verification-notes">
                  <h4>To verify</h4><ul>{job.unknown.map((note) => <li key={note}>{note}</li>)}</ul>
                </div>}
                <details className="evidence-quotes"><summary>View CV and vacancy evidence</summary>
                  {job.evidence.map((item) => <div className="evidence" key={item.skill}>
                    <b>{item.skill}</b><p><span>CV</span> {item.cv_quote}</p><p><span>Job</span> {item.vacancy_quote}</p>
                  </div>)}
                </details>
                <p className="muted small">Skill coverage {job.score}% · {job.scorer}.
                  {job.fit_score == null
                    ? " Overall fit has not been assessed."
                    : " Fit score is a model assessment based on the available requirements and your search preferences."}
                </p>
                {job.content_status === "indexed" && <p className="warning">
                  Indexed excerpt only. The full posting could not be opened.
                </p>}
                {job.sources.every((id) => id.startsWith("codex:")) && <p className="muted small">
                  Posting excerpts and facts were supplied by Codex. Confirm them on the vacancy page.
                </p>}
              </div>
            </details>
            <div className="job-footer">
              <a href={job.url} target="_blank" rel="noreferrer">Open vacancy ↗</a>
              {activeTab === "historical" && "first_seen_at" in job && <time className="muted small">
                {historyDate.format(new Date(String(job.first_seen_at)))}
              </time>}
              {activeTab === "latest" && <label className="checkbox"><input type="checkbox" checked={selected.includes(job.id)}
                disabled={!reviewPending || Boolean(busy)} onChange={(event) => {
                  setSelected(event.target.checked ? [...selected, job.id] : selected.filter((id) => id !== job.id));
                }} /> Keep</label>}
            </div>
          </article>)}
          </div>
        {activeTab === "latest" && run && <>
          {run.excluded.length > 0 && <details className="excluded"><summary>See excluded vacancies</summary>
            {run.excluded.map((job) => <p key={job.id}>{job.title} · {job.reasons.join(", ")}</p>)}
          </details>}
          {reviewPending && <button disabled={Boolean(busy)} onClick={() => void action("Saving review", async () => {
            const reviewed = await request<Run>("/api/runs/" + run.id + "/review", post({ selected }));
            setRun(reviewed); setSelected(reviewed.reviewed_ids);
            const saved = await request<History>("/api/history");
            setHistory(saved.jobs);
          })}>Confirm review · {selected.length} selected</button>}
        </>}
        </div>
        <footer className="pagination" aria-label="Result pagination">
          <span className="page-range" aria-live="polite">{total ? startIndex + 1 : 0}–{Math.min(startIndex + pageSize, total)} of {total}</span>
          <label className="page-size"><span className="sr-only">Results per page</span>
            <select value={pageSize} onChange={(event) => setPageSizeChoice(Number(event.target.value))}>
              {[5, 10, 20, 30, 50].map((size) => <option key={size} value={size}>{size} per page</option>)}
            </select>
          </label>
          <nav className="page-buttons" aria-label="Pages">
            <button className="secondary" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)} aria-label="Previous page">‹ <span>Previous</span></button>
            {Array.from({ length: Math.min(5, pageCount) }, (_, i) => Math.max(1, Math.min(currentPage - 2, pageCount - 4)) + i).map((number) =>
              <button key={number} className={number === currentPage ? "" : "secondary"} aria-current={number === currentPage ? "page" : undefined}
                onClick={() => setPage(number)}>{number}</button>)}
            <button className="secondary" disabled={currentPage === pageCount} onClick={() => setPage(currentPage + 1)} aria-label="Next page"><span>Next</span> ›</button>
          </nav>
        </footer>
      </section>
    </div>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
