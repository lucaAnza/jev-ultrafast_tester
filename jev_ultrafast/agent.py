"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text
from .model import decision_provider as resolve_decision_provider
from .questions import MAX_STEPS
from .run_log import log_decision, log_request


class Agent:
    def __init__(
        self,
        url,
        goals,
        *,
        decision_provider="typesafe",
        decision_log=None,
        record_dir=None,
        screenshots=False,
        model_screenshots=False,
        append_decision_log=False,
    ):
        """Open the test page and create the first observable state."""
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        provider = resolve_decision_provider(decision_provider)
        if model_screenshots and provider["name"] != "typesafe":
            raise ValueError("Screenshot model input is currently supported only by Jev AI.")
        self.browser = Browser(url)
        self.decision_log = Path(decision_log) if decision_log else None
        self.record_dir = Path(record_dir) if record_dir else None
        self.preview_screenshots = bool(screenshots)
        self.model_screenshots = bool(model_screenshots)
        self.screenshots = self.preview_screenshots or self.model_screenshots or bool(record_dir)
        try:
            page = self._observe()
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            decision_provider=provider["name"],
            decision_model=provider["model"],
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            harness_actions=[],
            token_usage={"requests": 0, "input_tokens": 0, "output_tokens": 0},
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            model_screenshots=self.model_screenshots,
            save_screenshots=bool(self.record_dir),
            screenshot_dir=str(self.record_dir) if self.record_dir else None,
            decision_log=str(self.decision_log) if self.decision_log else None,
        )
        if self.decision_log:
            self.decision_log.parent.mkdir(parents=True, exist_ok=True)
            if not append_decision_log:
                self.decision_log.write_text("", encoding="utf-8")
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            self._save_screenshot(page, 0)

    def _observe(self):
        """Read one page state with a screenshot whenever a selected mode needs it."""
        browser = getattr(self, "browser", None) or self.state["browser"]
        return browser.observe(
            screenshot=self.screenshots,
            screenshot_required=bool(self.record_dir),
        )

    def _save_screenshot(self, page, step):
        """Save one observed JPEG under the run's screenshot directory."""
        if not self.record_dir:
            return
        screenshot = page.get("screenshot")
        if not screenshot:
            raise RuntimeError("A required step screenshot was not captured.")
        (self.record_dir / f"{step:04d}.jpg").write_bytes(base64.b64decode(screenshot))

    def snapshot(self):
        """Return serializable agent state for the inspector and library caller."""
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def set_model_screenshots(self, enabled):
        """Apply image mode to the next decision and ensure its current view exists."""
        enabled = enabled is True
        if enabled and self.state["decision_provider"] != "typesafe":
            raise ValueError("Screenshot model input is currently supported only by Jev AI.")
        self.model_screenshots = enabled
        self.state["model_screenshots"] = enabled
        self.screenshots = self.preview_screenshots or enabled or bool(self.record_dir)
        if enabled and not self.state["page"].get("screenshot"):
            self.state["page"] = self._observe()

    def log_decision(self, decision, page, number):
        """Write one decision and its cumulative token usage to the run log."""
        totals = self.state.setdefault("token_usage", {"requests": 0, "input_tokens": 0, "output_tokens": 0})
        log_decision(getattr(self, "decision_log", None), decision, page, number, totals)

    def log_request(self, request, number):
        """Write the exact model request without its authorization token."""
        log_request(getattr(self, "decision_log", None), request, number)

    def _log_harness_action(self, action, text):
        """Record a sanitized command immediately before Browser Harness receives it."""
        kinds = {
            "click": "CLICK",
            "fill": "TYPE_TEXT",
            "select": "SELECT",
            "wait": "WAIT",
        }
        command = kinds.get(action["kind"], action["kind"].upper())
        if action["kind"] == "scroll":
            command = "SCROLL_DOWN" if action["delta"] > 0 else "SCROLL_UP"
        started_at = self.state.get("started_at")
        entry = {
            "step": len(self.state.setdefault("harness_actions", [])) + 1,
            "action_step": len(self.state["history"]) + 1,
            "command": command,
            "action": action["label"],
            "action_id": action["id"],
            "node": action.get("node"),
            "text": text if action["kind"] == "fill" else None,
            "value": action.get("value") if action["kind"] == "select" else None,
            "delta": action.get("delta") if action["kind"] == "scroll" else None,
            "status": "sent",
            "sent_ms": round((time.perf_counter() - started_at) * 1000) if started_at else 0,
        }
        self.state["harness_actions"].append(entry)
        return entry

    def command(self, name, body=None):
        """Run one predict, act, or combined tick command."""
        body = body or {}
        state = self.state
        if name in {"predict", "tick"} and "model_screenshots" in body:
            self.set_model_screenshots(body["model_screenshots"])
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = self._observe()
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a test first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = self._observe()
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh test.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the test's model-call budget")
            decision_number = len(state["decisions"]) + 1
            state["decision"] = choose(
                state["page"],
                state["goal"],
                state["history"],
                state["decision_provider"],
                include_screenshot=self.model_screenshots,
                on_request=lambda request: self.log_request(request, decision_number),
            )
            self.log_decision(state["decision"], state["page"], decision_number)
            selected_action = next(
                (action for action in state["page"]["actions"] if action["id"] == state["decision"]["choice"]),
                None,
            )
            state["decisions"].append(
                {
                    **state["decision"],
                    "action_label": selected_action["label"] if selected_action else state["decision"]["choice"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action test budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            harness_entry = None

            def log_before_send(sent_action, sent_text):
                """Persist the exact high-level command after freshness checks pass."""
                nonlocal harness_entry
                harness_entry = self._log_harness_action(sent_action, sent_text)

            # Browser.act logs only after its final freshness check and immediately before input.
            state["browser"].act(action, page, text=text, on_send=log_before_send)
            if harness_entry:
                harness_entry["status"] = "confirmed"
                harness_entry["confirmed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = self._observe()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                self._save_screenshot(state["page"], len(state["history"]))
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        """Yield states until the decision model reports completion or a blocker."""
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        """Close the browser tab owned by this agent."""
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
