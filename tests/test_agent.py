"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import base64
import json
import os
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint
from jev_ultrafast.utils.config import load_environment
from jev_ultrafast.demo import start_url


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("url", ["https://example.test/path", "http://127.0.0.1:8000/"])
def test_start_url_accepts_websites(url):
    assert start_url(f"  {url}  ") == url


@pytest.mark.parametrize("url", ["", "example.test", "file:///tmp/page.html", "javascript:alert(1)"])
def test_start_url_rejects_non_web_urls(url):
    with pytest.raises(ValueError, match="valid http"):
        start_url(url)


def test_environment_file_fills_an_inherited_empty_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-file\n")

    load_environment(tmp_path / ".env")

    assert os.environ["TYPESAFE_API_KEY"] == "from-file"


def test_environment_file_does_not_replace_a_nonempty_export(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "exported")
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-file\n")

    load_environment(tmp_path / ".env")

    assert os.environ["TYPESAFE_API_KEY"] == "exported"


def test_decision_provider_loads_its_key_without_demo_startup(monkeypatch, tmp_path):
    monkeypatch.setattr("jev_ultrafast.utils.config.PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=provider-key\n")

    provider = model.decision_provider("typesafe")

    assert provider["key"] == "provider-key"


def test_decision_provider_finds_project_env_from_logs(monkeypatch, tmp_path):
    monkeypatch.setattr("jev_ultrafast.utils.config.PROJECT_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    monkeypatch.chdir(tmp_path / "logs")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=project-key\n")

    assert model.decision_provider("typesafe")["key"] == "project-key"


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid decision"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_openrouter_adapter_uses_drex_decisions_endpoint(monkeypatch):
    calls = []

    def post(url, key, body):
        calls.append((url, key, body))
        return {
            "model": "nace-ai/drex-max",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(body["questions"]["click_target"]["criteria"], "2"),
            },
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test")
    monkeypatch.setattr(model, "post_json", post)

    decision = model.choose(page(), "Click Go", [], "openrouter")

    assert decision["choice"] == "e3"
    assert calls[0][0] == "https://openrouter.ai/api/alpha/decisions"
    assert calls[0][1] == "openrouter-test"
    assert calls[0][2]["model"] == "nace-ai/drex-max"
    assert set(calls[0][2]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_typesafe_screenshot_is_shared_once_in_state(monkeypatch):
    calls = []
    sent_requests = []

    def post(_url, _key, body, headers):
        calls.append((body, headers))
        return {
            "model": "jev-1.14.0",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(body["questions"]["click_target"]["criteria"], "2"),
            },
        }

    observed = page()
    observed["screenshot"] = base64.b64encode(b"jpeg bytes").decode()
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)

    result = model.choose(
        observed,
        "Click Go",
        [],
        include_screenshot=True,
        on_request=sent_requests.append,
    )

    body, headers = calls[0]
    reference = body["state"]["browser_view"]
    assert body["assets"][reference]["data_base64"] == observed["screenshot"]
    assert headers == {"TypeSafe-Danger-Zone-Images": "true"}
    assert result["request"]["assets"][reference]["data_base64"].startswith("<omitted ")
    assert sent_requests[0]["body"]["assets"][reference]["data_base64"] == observed["screenshot"]
    assert sent_requests[0]["headers"] == headers
    assert "Authorization" not in sent_requests[0]["headers"]


def test_screenshot_mode_requires_typesafe_and_a_captured_image(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    with pytest.raises(ValueError, match="supported only by Jev AI"):
        model.choose(page(), "Click Go", [], "openrouter", include_screenshot=True)
    with pytest.raises(ValueError, match="screenshot is unavailable"):
        model.choose(page(), "Click Go", [], include_screenshot=True)


@pytest.mark.parametrize("value", [None, "", "   ", "chat"])
def test_decision_provider_requires_supported_choice(value):
    with pytest.raises(ValueError, match="Choose Jev AI or OpenRouter"):
        model.decision_provider(value)


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid decision"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_text_helper_can_generate_synthetic_values_when_authorized(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"tester@example.test"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = {
        "goal": "Test login using synthetic test data",
        "field": {"label": "Email", "role": "textbox", "value": ""},
    }

    assert model.field_text(context)[0] == "tester@example.test"
    system_prompt = post.call_args.args[2]["messages"][0]["content"]
    assert "synthetic test data" in system_prompt


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.preview_screenshots = False
    a.record_dir = None
    a.model_screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "decision_provider": "typesafe",
        "history": [],
        "harness_actions": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_step_screenshot_is_saved_with_a_stable_sequence(tmp_path):
    runner = loop.Agent.__new__(loop.Agent)
    runner.record_dir = tmp_path
    encoded = base64.b64encode(b"jpeg bytes").decode()

    runner._save_screenshot({"screenshot": encoded}, 3)

    assert (tmp_path / "0003.jpg").read_bytes() == b"jpeg bytes"


def test_exact_model_request_is_written_without_authorization(runner, tmp_path):
    runner.decision_log = tmp_path / "decisions.log"
    runner.decision_log.write_text("")
    request = {
        "url": "https://api.typesafe.ai/v1/systemone",
        "headers": {"TypeSafe-Danger-Zone-Images": "true"},
        "body": {"assets": {"view": {"data_base64": "full-image-data"}}},
    }

    runner.log_request(request, 1)

    saved = runner.decision_log.read_text()
    assert "Decision 1 request:" in saved
    assert "full-image-data" in saved
    assert "TypeSafe-Danger-Zone-Images" in saved
    assert "Authorization" not in saved


def test_agent_rejects_image_mode_for_openrouter_before_opening_browser():
    with pytest.raises(ValueError, match="supported only by Jev AI"):
        loop.Agent(
            "https://example.test",
            "Test it",
            decision_provider="openrouter",
            model_screenshots=True,
        )


def test_image_mode_is_applied_to_each_prediction(runner, monkeypatch):
    selected = decision("e3")
    choose = Mock(return_value=selected)
    monkeypatch.setattr(loop, "choose", choose)
    runner.state["page"]["screenshot"] = base64.b64encode(b"jpeg bytes").decode()
    runner.state["decision"] = None
    runner.state["status"] = "ready"

    runner.command("predict", {"model_screenshots": True})

    assert runner.state["model_screenshots"] is True
    assert choose.call_args.kwargs["include_screenshot"] is True


def test_image_mode_can_be_disabled_during_a_run(runner, monkeypatch):
    choose = Mock(return_value=decision("e3"))
    monkeypatch.setattr(loop, "choose", choose)
    runner.model_screenshots = True
    runner.state["model_screenshots"] = True
    runner.state["decision"] = None
    runner.state["status"] = "ready"

    runner.command("predict", {"model_screenshots": False})

    assert runner.state["model_screenshots"] is False
    assert choose.call_args.kwargs["include_screenshot"] is False


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_harness_command_is_logged_before_mutation_and_then_confirmed(runner):
    runner.state["decision"] = decision("e3")

    def act(action, _page, text=None, on_send=None):
        on_send(action, text)
        assert runner.state["harness_actions"][-1]["status"] == "sent"

    runner.state["browser"].act.side_effect = act

    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    entry = runner.state["harness_actions"][-1]
    assert entry["command"] == "CLICK"
    assert entry["action"] == "Go"
    assert entry["status"] == "confirmed"


def test_sent_harness_command_survives_an_execution_error(runner):
    runner.state["decision"] = decision("e3")

    def act(action, _page, text=None, on_send=None):
        on_send(action, text)
        raise RuntimeError("Harness connection failed")

    runner.state["browser"].act.side_effect = act

    with pytest.raises(RuntimeError, match="Harness connection failed"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    assert runner.state["harness_actions"][-1]["status"] == "sent"
    assert runner.state["history"] == []


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_optional_screenshot_timeout_preserves_observed_state(monkeypatch):
    import jev_ultrafast.browser as browser

    def cdp(method, **params):
        if method == "Runtime.evaluate":
            return {"result": {"value": page()}}
        assert method == "Page.captureScreenshot"
        assert params["_response_timeout"] == browser.SCREENSHOT_IPC_RESPONSE_TIMEOUT_SECONDS
        raise TimeoutError("Page.captureScreenshot timed out")

    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": True})

    assert actual["actions"] == page()["actions"]
    assert actual["screenshot"] is None


def test_recording_requires_a_screenshot(monkeypatch):
    import jev_ultrafast.browser as browser

    def cdp(method, **_params):
        if method == "Runtime.evaluate":
            return {"result": {"value": page()}}
        raise TimeoutError("Page.captureScreenshot timed out")

    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(TimeoutError, match="captureScreenshot"):
        browser_operation(
            {
                "operation": "observe",
                "session": "test",
                "screenshot": True,
                "screenshot_required": True,
            }
        )


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    on_send = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book", on_send=on_send)
    operation.assert_not_called()
    on_send.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()
