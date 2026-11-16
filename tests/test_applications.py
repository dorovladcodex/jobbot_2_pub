from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import subprocess
import tomllib
import unittest
from unittest.mock import patch

from jobbot.applications import ApplicationStore
from jobbot.application_sync import build_application_sync, classify_messages
from jobbot.mailbox import cli_failure_message, failure_details, gmail_profiles, gmail_trace, mailbox_options, read_mailbox, tool_call_summary


EMAIL = "candidate@example.com"
JOB = {"id": "job-one", "company": "Example", "title": "Data Engineer", "url": "https://example.com/jobs/1"}


def message(message_id="mail0001", thread_id="thread01", received_at="2026-11-15T10:00:00+00:00",
            body="We have received your application for Data Engineer at Example."):
    return {"message_id": message_id, "thread_id": thread_id, "received_at": received_at,
            "subject": "Your application", "body": body}


def event(raw, status="applied", title="Data Engineer", job_url=None):
    return {"message_id": raw["message_id"], "thread_id": raw["thread_id"], "received_at": raw["received_at"],
            "subject": raw["subject"], "company": "Example", "title": title, "job_url": job_url,
            "status": status, "evidence_quote": raw["body"], "interview_at": None}


def batch(messages, cursor=""):
    return {"mailbox": EMAIL, "status": "ok", "messages": messages, "next_page_token": cursor}


def gmail_call(tool, result, *, link_id="link_candidate", status="completed"):
    return {"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "codex_apps", "tool": "gmail_" + tool,
        "arguments": {"link_id": link_id}, "status": status, "result": result,
    }}


def tool_policy(options):
    override = next(value for value in options if value.startswith("apps.connector_gmail.tools="))
    return tomllib.loads(override)["apps"]["connector_gmail"]["tools"]


class ApplicationTests(unittest.TestCase):
    def test_existing_application_matches_a_later_search_and_survives_restart(self):
        raw = message()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite"
            with closing(ApplicationStore(path)) as store:
                store.save(EMAIL, batch([raw]), [{**event(raw), "company": "Example GmbH",
                           "title": "Data Engineer (m/f/d)", "job_url": "https://board.example/job/abc"}],
                           [], "2026-05-01")
                self.assertIsNone(store.list()["applications"][0]["job_id"])
                self.assertIn(JOB["id"], store.linked_job_ids([JOB]))
            with closing(ApplicationStore(path)) as store:
                application = store.list()["applications"][0]
                self.assertEqual(application["job_id"], JOB["id"])
                self.assertEqual(application["match_state"], "matched")
                self.assertEqual(len(application["events"]), 1)

    def test_ambiguous_postings_and_different_roles_are_not_automatically_linked(self):
        raw = message()
        with closing(ApplicationStore(":memory:")) as store:
            store.save(EMAIL, batch([raw]), [event(raw)], [], "2026-05-01")
            store.reconcile([JOB, {**JOB, "id": "second-posting", "url": "https://example.com/jobs/2"}])
            self.assertIsNone(store.list()["applications"][0]["job_id"])
            store.reconcile([{**JOB, "title": "Senior DataOps Engineer"}])
            self.assertIsNone(store.list()["applications"][0]["job_id"])

    def test_matching_does_not_merge_distinct_application_urls_by_title_alone(self):
        first, second = message(), message("mail0002", "thread02")
        for available_at_sync in ([], [JOB]):
            with self.subTest(available_at_sync=bool(available_at_sync)), closing(ApplicationStore(":memory:")) as store:
                store.save(EMAIL, batch([first, second]), [
                    event(first, job_url="https://board.example/jobs/1"),
                    event(second, "rejected", job_url="https://board.example/jobs/2"),
                ], available_at_sync, "2026-05-01")
                store.reconcile([JOB])
                applications = store.list()["applications"]
                self.assertEqual(len(applications), 2)
                self.assertEqual({app["status"] for app in applications}, {"applied", "rejected"})

    def test_exact_url_can_match_a_missing_role_without_company_only_guessing(self):
        raw = message()
        with closing(ApplicationStore(":memory:")) as store:
            store.save(EMAIL, batch([raw]), [event(raw, title="")], [JOB], "2026-05-01")
            store.reconcile([JOB])
            self.assertIsNone(store.list()["applications"][0]["job_id"])
        with closing(ApplicationStore(":memory:")) as store:
            store.save(EMAIL, batch([raw]), [event(raw, title="", job_url=JOB["url"] + "?utm_source=email")],
                       [JOB], "2026-05-01")
            self.assertEqual(store.list()["applications"][0]["job_id"], JOB["id"])

    def test_sync_is_idempotent_and_late_receipt_does_not_replace_rejection(self):
        receipt = message()
        rejection = message("mail0002", received_at="2026-11-16T10:00:00+00:00", body="Your application was unsuccessful.")
        late_receipt = message("mail0003", received_at="2026-11-17T10:00:00+00:00")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "applications.sqlite"
            with closing(ApplicationStore(path)) as store:
                store.save(EMAIL, batch([receipt]), [event(receipt)], [JOB], "2026-05-01")
                store.save(EMAIL, batch([receipt]), [event(receipt)], [JOB], "2026-05-01")
                self.assertEqual(len(store.list()["applications"][0]["events"]), 1)
                store.save(EMAIL, batch([rejection]), [event(rejection, "rejected")], [JOB], "2026-05-01")
                store.save(EMAIL, batch([late_receipt]), [event(late_receipt)], [JOB], "2026-05-01")
            with closing(ApplicationStore(path)) as store:
                apps = store.list()["applications"]
                self.assertEqual(len(apps), 1)
                self.assertEqual(apps[0]["status"], "rejected")
                self.assertEqual(len(apps[0]["events"]), 3)
                self.assertEqual(apps[0]["job_id"], JOB["id"])
                self.assertIn("authuser=candidate%40example.com", apps[0]["events"][0]["email_url"])
                self.assertIn(JOB["id"], store.linked_job_ids())

    def test_missing_role_requires_review_and_manual_link_preserves_all_events(self):
        second_job = {**JOB, "id": "job-two", "title": "AI Engineer", "url": "https://example.com/jobs/2"}
        first, second = message(), message("mail0002", "thread02")
        vague = message("mail0003", "thread03", body="We regret to inform you your application was unsuccessful.")
        with closing(ApplicationStore(":memory:")) as store:
            store.save(EMAIL, batch([first, second]), [event(first), event(second, title="AI Engineer")],
                       [JOB, second_job], "2026-05-01")
            store.save(EMAIL, batch([vague]), [event(vague, "rejected", title="")],
                       [JOB, second_job], "2026-05-01")
            apps = store.list()["applications"]
            review = next(app for app in apps if app["match_state"] == "needs_review")
            self.assertEqual(len(apps), 3)
            store.link(review["id"], JOB)
            apps = store.list()["applications"]
            self.assertEqual(len(apps), 2)
            linked = next(app for app in apps if app["job_id"] == JOB["id"])
            self.assertEqual(linked["status"], "rejected")
            self.assertEqual(len(linked["events"]), 2)

    def test_pagination_and_mailbox_isolation(self):
        raw = message()
        with closing(ApplicationStore(":memory:")) as store:
            store.save(EMAIL, batch([raw], "next-page"), [event(raw)], [JOB], "2026-05-01")
            self.assertEqual(store.cursor(EMAIL), ("2026-05-01", "next-page"))
            self.assertEqual(store.seen_messages("another@example.com"), set())
            store.save("another@example.com", batch([raw]), [event(raw, "rejected")], [JOB], "2026-05-01")
            apps = store.list()["applications"]
            self.assertEqual(len(apps), 2)
            self.assertEqual({app["mailbox"] for app in apps}, {EMAIL, "another@example.com"})

    def test_same_title_with_different_posting_urls_stays_separate(self):
        second_job = {**JOB, "id": "job-two", "url": "https://example.com/jobs/2"}
        first = message()
        second = message("mail0002", body="We received your other application.")
        with closing(ApplicationStore(":memory:")) as store:
            store.save(EMAIL, batch([first]), [event(first, job_url=JOB["url"])],
                       [JOB, second_job], "2026-05-01")
            store.save(EMAIL, batch([second]), [event(second, "rejected", job_url=second_job["url"])],
                       [JOB, second_job], "2026-05-01")
            apps = store.list()["applications"]
            self.assertEqual(len(apps), 2)
            self.assertEqual({app["job_id"]: app["status"] for app in apps},
                             {"job-one": "applied", "job-two": "rejected"})

    def test_model_evidence_is_validated_and_unscheduled_invite_is_not_scheduled(self):
        raw = message(body="Please choose a time for an interview.")
        output = {"events": [{
            "message_id": raw["message_id"], "company": "Example", "title": "Data Engineer",
            "job_url": "https://invented.example/job", "status": "interview_scheduled",
            "evidence_quote": raw["body"], "interview_at": None,
        }]}
        with patch("jobbot.application_sync.run_structured", return_value=output):
            events = classify_messages([raw])
        self.assertEqual(events[0]["status"], "interview_invitation")
        self.assertIsNone(events[0]["job_url"])
        output["events"][0]["evidence_quote"] = "Fabricated rejection"
        with patch("jobbot.application_sync.run_structured", return_value=output):
            with self.assertRaisesRegex(ValueError, "unsupported evidence"):
                classify_messages([raw])

    def test_agreed_interview_time_preserves_timezone(self):
        raw = message(body="Your interview is confirmed for 20 November at 13:00 CET.")
        output = {"events": [{
            "message_id": raw["message_id"], "company": "Example", "title": "Data Engineer",
            "job_url": None, "status": "interview_scheduled", "evidence_quote": raw["body"],
            "interview_at": "2026-11-20T13:00:00+01:00",
        }]}
        with patch("jobbot.application_sync.run_structured", return_value=output):
            events = classify_messages([raw])
        self.assertEqual(events[0]["status"], "interview_scheduled")
        self.assertEqual(events[0]["interview_at"], "2026-11-20T13:00:00+01:00")

    def test_independent_graph_saves_only_after_success_and_skips_processed_mail(self):
        raw = message()
        save = []
        graph = build_application_sync(lambda state: save.append(state) or {"saved": True})
        state = {"mailbox": EMAIL, "since": "2026-05-01", "cursor": "", "processed_ids": [raw["message_id"]]}
        with patch("jobbot.application_sync.read_mailbox", return_value=batch([raw])), \
                patch("jobbot.application_sync.run_structured") as model:
            result = graph.invoke(state)
        model.assert_not_called()
        self.assertTrue(result["result"]["saved"])
        self.assertEqual(save[0]["events"], [])
        save.clear()
        with patch("jobbot.application_sync.read_mailbox", side_effect=ValueError("Account not connected")):
            with self.assertRaises(ValueError):
                graph.invoke(state)
        self.assertEqual(save, [])

    def test_read_permissions_and_trace_cannot_be_faked_by_final_text(self):
        options = mailbox_options("connector_gmail", {})
        self.assertIn("apps.connector_gmail.default_tools_enabled=false", options)
        self.assertTrue(tool_policy(options)["gmail.get_profile"]["enabled"])
        self.assertFalse(any("send" in option or "delete" in option for option in options))
        self.assertEqual(gmail_trace('{"type":"item.completed","item":{"type":"agent_message","text":"read_email mail0001"}}'),
                         (set(), False))
        self.assertEqual(gmail_trace('1\nnull\n{"item":null}'), (set(), False))
        trace = [
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "codex_apps",
             "tool": "gmail_search_emails", "status": "completed", "result": {}}},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "codex_apps",
             "tool": "gmail_read_email", "status": "completed", "result": {
                 "content": [{"type": "text", "text": json.dumps({"id": "mail0001"})}],
             }}},
        ]
        self.assertEqual(gmail_trace("\n".join(json.dumps(item) for item in trace)), ({"mail0001"}, True))

    def test_mailbox_model_can_be_selected_without_changing_desktop_config(self):
        with patch.dict("os.environ", {"JOBBOT_GMAIL_MODEL": "available-model"}):
            options = mailbox_options("connector_gmail", {"model": "desktop-model"})
        self.assertIn('model="available-model"', options)
        self.assertNotIn('model="desktop-model"', options)

    def test_request_failure_takes_priority_over_incompatible_cache_diagnostic(self):
        reason = "The 'desktop-model' model is not supported when using Codex with a ChatGPT account."
        payload = {"type": "error", "status": 400, "error": {
            "type": "invalid_request_error", "message": reason, "code": "model_not_found",
        }}
        stdout = "\n".join(json.dumps(item) for item in [
            {"type": "error", "message": json.dumps(payload)},
            {"type": "turn.failed", "error": {"message": json.dumps(payload)}},
        ])
        stderr = "ERROR codex_models_manager::cache: failed to load models cache: missing field `base_instructions`"
        self.assertEqual(failure_details(stderr, stdout).splitlines()[-1], reason)
        result = cli_failure_message(stderr, stdout, 1)
        self.assertIn(reason, result)
        self.assertIn("JOBBOT_GMAIL_MODEL", result)
        self.assertNotIn("base_instructions", result)

    def test_existing_plugin_config_is_loaded_with_prefixed_read_tools_and_writes_disabled(self):
        config = {
            "plugins": {"gmail@openai-curated": {"enabled": True}, "other@marketplace": {"enabled": True}},
            "mcp_servers": {"local_tools": {"command": "node"}},
            "apps": {
                "connector_gmail": {"tools": {
                    "gmail_send_email": {"enabled": True}, "gmail.send_email": {"enabled": True},
                    "send_email": {"enabled": True},
                }},
                "other_connector": {"enabled": True},
            },
        }
        options = mailbox_options("connector_gmail", config)
        policy = tool_policy(options)
        for action in ("get_profile", "search_emails", "read_email"):
            for name in (action, "gmail." + action, "gmail_" + action):
                self.assertTrue(policy[name]["enabled"], name)
        for name in ("send_email", "gmail.send_email", "gmail_send_email"):
            self.assertFalse(policy[name]["enabled"], name)
        self.assertIn("apps.connector_gmail.omit_tools_from=[]", options)
        plugin_override = next(value for value in options if value.startswith("plugins="))
        plugins = tomllib.loads(plugin_override)["plugins"]
        self.assertFalse(plugins["other@marketplace"]["enabled"])
        self.assertTrue(plugins["gmail@openai-curated"]["enabled"])
        self.assertIn("mcp_servers.local_tools.enabled=false", options)
        self.assertIn("apps.other_connector.enabled=false", options)

    def test_mailbox_launch_keeps_user_config_and_verifies_read_messages(self):
        raw = {**message(), "sender": "jobs@example.com"}
        trace = [
            gmail_call("get_profile", {"content": [{"type": "text", "text": "Action completed."}],
                                        "structured_content": {"email": EMAIL}}),
            gmail_call("search_emails", {}),
            gmail_call("read_email", {"id": raw["message_id"]}),
        ]

        def launch(command, **kwargs):
            self.assertNotIn("--ignore-user-config", command)
            self.assertIn("max_results=25", kwargs["input"])
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            result_path = Path(command[command.index("-o") + 1])
            result_path.write_text(json.dumps(batch([raw])), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(item) for item in trace), "")

        with TemporaryDirectory() as directory, patch.dict("os.environ", {"CODEX_HOME": directory}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"), \
                patch("jobbot.mailbox.subprocess.run", side_effect=launch):
            result = read_mailbox(EMAIL, "2026-05-01")
        self.assertEqual(result["messages"][0]["message_id"], raw["message_id"])

    def test_profiles_support_structured_results_and_json_arguments_but_not_final_prose(self):
        for key in ("structured_content", "structuredContent"):
            profile = gmail_call("get_profile", {key: {"email": EMAIL}, "content": [
                {"type": "text", "text": "Action completed."},
            ]})
            profile["item"]["tool"] = "gmail.get_profile"
            self.assertEqual(gmail_profiles(json.dumps(profile)), {"link_candidate": EMAIL})
        profile = gmail_call("get_profile", {"content": [{"type": "text", "text": json.dumps({
            "emailAddress": EMAIL.upper(),
        })}]})
        profile["item"]["arguments"] = json.dumps({"link_id": "link_candidate"})
        self.assertEqual(gmail_profiles(json.dumps(profile)), {"link_candidate": EMAIL})
        profile["item"]["result"]["isError"] = True
        self.assertEqual(gmail_profiles(json.dumps(profile)), {})
        self.assertEqual(gmail_profiles(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps({"email": EMAIL, "link_id": "link_candidate"}),
        }})), {})

    def test_model_cannot_verify_an_account_or_reuse_reads_from_another_mailbox(self):
        raw = {**message(), "sender": "jobs@example.com"}
        valid = [gmail_call("get_profile", {"structuredContent": {"email": EMAIL}}),
                 gmail_call("search_emails", {}), gmail_call("read_email", {"id": raw["message_id"]})]
        cases = [
            (valid[1:], "get_profile", "account_verification"),
            ([gmail_call("get_profile", {"email": "other@example.com"}), *valid[1:]],
             "get_profile", "account_verification"),
            ([valid[0], gmail_call("search_emails", {}, link_id="link_other"), valid[2]],
             "requested account", "mailbox_evidence"),
            ([*valid[:2], gmail_call("read_email", {"id": raw["message_id"]}, link_id="link_other")],
             "requested account", "mailbox_evidence"),
        ]
        with TemporaryDirectory() as directory, patch.dict("os.environ", {
                "CODEX_HOME": directory, "JOBBOT_DB": str(Path(directory) / "test.sqlite")}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"):
            for trace, expected, stage in cases:
                def launch(command, **kwargs):
                    Path(command[command.index("-o") + 1]).write_text(json.dumps(batch([raw])), encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, trace)), "")

                with self.subTest(stage=stage, trace=trace), \
                        patch("jobbot.mailbox.subprocess.run", side_effect=launch):
                    with self.assertRaisesRegex(ValueError, expected):
                        read_mailbox(EMAIL, "2026-05-01")
                    diagnostic = json.loads((Path(directory) / "gmail-sync-last-error.json").read_text(encoding="utf-8"))
                    self.assertEqual(diagnostic["stage"], stage)
                    self.assertNotIn(EMAIL, diagnostic["technical_error"])

    def test_verified_empty_mailbox_is_a_successful_sync(self):
        trace = [gmail_call("get_profile", {"email": EMAIL}), gmail_call("search_emails", {})]

        def launch(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text(json.dumps(batch([])), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, trace)), "")

        with TemporaryDirectory() as directory, patch.dict("os.environ", {"CODEX_HOME": directory}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"), \
                patch("jobbot.mailbox.subprocess.run", side_effect=launch):
            self.assertEqual(read_mailbox(EMAIL, "2026-05-01")["messages"], [])

    def test_default_connection_without_link_id_still_requires_exact_profile_match(self):
        trace = [gmail_call("get_profile", {"emailAddress": EMAIL}), gmail_call("search_emails", {})]
        for call in trace:
            call["item"]["arguments"] = {}
        stdout = "\n".join(map(json.dumps, trace))
        self.assertEqual(gmail_profiles(stdout), {None: EMAIL})
        self.assertEqual(gmail_trace(stdout, link_ids={None}), (set(), True))
        self.assertEqual(gmail_trace(stdout, link_ids={"another_link"}), (set(), False))

        def launch(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text(json.dumps(batch([])), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with TemporaryDirectory() as directory, patch.dict("os.environ", {
                "CODEX_HOME": directory, "JOBBOT_DB": str(Path(directory) / "test.sqlite")}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"), \
                patch("jobbot.mailbox.subprocess.run", side_effect=launch):
            self.assertEqual(read_mailbox(EMAIL, "2026-05-01")["messages"], [])
            stdout = stdout.replace(EMAIL, "other@example.com")
            with self.assertRaisesRegex(ValueError, "did not confirm"):
                read_mailbox(EMAIL, "2026-05-01")

    def test_no_profile_is_not_reported_as_a_missing_account_and_keeps_tool_diagnostics(self):
        failed = gmail_call("get_profile", {"content": []}, status="failed")
        failed["item"]["tool"] = "gmail.get_profile"
        failed["item"]["error"] = {"message": "Tool unavailable"}
        trace = json.dumps(failed)

        def launch(command, **kwargs):
            result = {**batch([]), "status": "not_connected"}
            Path(command[command.index("-o") + 1]).write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, trace, "")

        with TemporaryDirectory() as directory, patch.dict("os.environ", {
                "CODEX_HOME": directory, "JOBBOT_DB": str(Path(directory) / "test.sqlite")}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"), \
                patch("jobbot.mailbox.subprocess.run", side_effect=launch):
            with self.assertRaisesRegex(ValueError, "did not return a verified Gmail profile"):
                read_mailbox(EMAIL, "2026-05-01")
            diagnostic = json.loads((Path(directory) / "gmail-sync-last-error.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["stage"], "profile_not_checked")
            self.assertEqual(diagnostic["tool_calls"], [{
                "server": "codex_apps", "tool": "gmail.get_profile", "status": "failed", "error": "Tool unavailable",
            }])

    def test_tool_diagnostic_excludes_mail_results_and_arguments(self):
        call = gmail_call("read_email", {"body": "PRIVATE BODY"})
        call["item"]["arguments"]["query"] = "PRIVATE QUERY"
        summary = json.dumps(tool_call_summary(json.dumps(call)))
        self.assertIn("gmail_read_email", summary)
        self.assertNotIn("PRIVATE", summary)
        self.assertNotIn("link_candidate", summary)

    def test_mailbox_startup_and_schema_errors_have_distinct_diagnostics(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {
                "CODEX_HOME": directory, "JOBBOT_DB": str(Path(directory) / "test.sqlite")}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"):
            for stderr, expected in [
                ("failed to open state db", "state database"),
                ("invalid_json_schema: rejected", "output schema"),
                ("plugin gmail not found", "load Gmail"),
            ]:
                with self.subTest(stderr=stderr), patch("jobbot.mailbox.subprocess.run", side_effect=
                    subprocess.CalledProcessError(1, "codex", output="", stderr=stderr)):
                    with self.assertRaisesRegex(ValueError, expected):
                        read_mailbox(EMAIL, "2026-05-01")
            with patch("jobbot.mailbox.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 240)):
                with self.assertRaisesRegex(ValueError, "4 minutes"):
                    read_mailbox(EMAIL, "2026-05-01")
            diagnostic = json.loads((Path(directory) / "gmail-sync-last-error.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["stage"], "timeout")

    def test_json_cli_errors_are_exposed_and_saved_without_mail_or_credentials(self):
        stdout = "\n".join(json.dumps(item) for item in [
            {"type": "item.completed", "item": {"result": {"body": "PRIVATE MAIL BODY"}}},
            {"type": "error", "message": "Transient error; retrying"},
            {"type": "turn.failed", "error": {"message": "Model request failed: stream disconnected"}},
        ])
        stderr = ('ERROR authorization="Bearer abcdef" api_key=sk-test123 '
                  'password="secret with spaces" account=candidate@example.com')
        details = failure_details(stderr, stdout)
        self.assertIn("stream disconnected", details)
        for private in ("PRIVATE MAIL BODY", "abcdef", "sk-test123", "secret with spaces", EMAIL):
            self.assertNotIn(private, details)
        self.assertIn("stream disconnected", cli_failure_message("", stdout, 1))
        with TemporaryDirectory() as directory, patch.dict("os.environ", {
                "CODEX_HOME": directory, "JOBBOT_DB": str(Path(directory) / "test.sqlite")}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"), \
                patch("jobbot.mailbox.subprocess.run", side_effect=subprocess.CalledProcessError(
                    1, "codex", output=stdout, stderr=stderr)):
            with self.assertRaisesRegex(ValueError, "exited with code 1"):
                read_mailbox(EMAIL, "2026-05-01")
            diagnostic = json.loads((Path(directory) / "gmail-sync-last-error.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["stage"], "codex_process")
            self.assertEqual(diagnostic["exit_code"], 1)
            self.assertEqual(diagnostic["codex_binary"], "codex")
            self.assertEqual(diagnostic["technical_error"], details)

    def test_diagnostic_write_failure_does_not_hide_original_error(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"CODEX_HOME": directory}), \
                patch("jobbot.mailbox.gmail_app_id", return_value="connector_gmail"), \
                patch("jobbot.mailbox.codex_binary", return_value="codex"), \
                patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only directory")), \
                patch("jobbot.mailbox.subprocess.run", side_effect=subprocess.CalledProcessError(
                    1, "codex", output='', stderr='Error: model request failed')):
            with self.assertRaisesRegex(ValueError, "model request failed"):
                read_mailbox(EMAIL, "2026-05-01")
