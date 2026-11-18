"""A System One provider makes choices; an optional text model writes field values."""

import json
import math
import os
import time
import uuid

import httpx

from .questions import (
    QUESTION,
    QUESTION_OPERATION,
    QUESTION_TEXT_VALUE,
    TARGET_OPERATION_CRITERIA,
    TARGET_QUESTIONS,
    TERMINAL_OPERATION_CRITERIA,
)
from .utils.config import load_environment

CLIENT = httpx.Client(http2=True, timeout=25)

DECISION_PROVIDERS = {
    "typesafe": {
        "url": "https://api.typesafe.ai/v1/systemone",
        "key_name": "TYPESAFE_API_KEY",
        "model": "jev-latest",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/alpha/decisions",
        "key_name": "OPENROUTER_API_KEY",
        "model": "nace-ai/drex-max",
    },
}


def decision_provider(name="typesafe"):
    """Resolve the compatible System One endpoint without changing the decision contract."""
    provider = name.strip().lower() if isinstance(name, str) else ""
    if provider not in DECISION_PROVIDERS:
        raise ValueError("Choose Jev AI or OpenRouter as the decision provider.")
    settings = DECISION_PROVIDERS[provider]
    environment_file = load_environment()
    key = os.environ.get(settings["key_name"], "").strip()
    if not key:
        print(
            f"[config] {settings['key_name']} missing; cwd={os.getcwd()} "
            f"env_file={environment_file} exists={environment_file.is_file()}",
            flush=True,
        )
    return {
        "name": provider,
        "url": settings["url"],
        "key": key or None,
        "model": settings["model"],
        "key_name": settings["key_name"],
    }


def post_json(url, key, body, extra_headers=None):
    """Post one model request with authorization and optional feature headers."""
    headers = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers=headers)
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            print("[ERROR LOG] response:", response.text)
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    """Reject answers that do not exactly match the code-owned choice set."""
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid decision response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def _add_screenshot(body, state):
    """Attach the observed JPEG to the shared System One state."""
    screenshot = state.get("screenshot")
    if not screenshot:
        raise ValueError("The current browser screenshot is unavailable; choose again or disable image mode.")
    reference = f"jev-browser-view-{uuid.uuid4().hex}"
    body["state"]["browser_view"] = reference
    body["assets"] = {
        reference: {"type": "image", "format": "jpg", "data_base64": screenshot},
    }


def _inspectable_request(body):
    """Copy a request for logs and the UI without duplicating large image bytes."""
    request = {**body, "state": dict(body["state"])}
    if "assets" in body:
        request["assets"] = {
            key: {**asset, "data_base64": f"<omitted {len(asset['data_base64'])} base64 characters>"}
            for key, asset in body["assets"].items()
        }
    return request


def choose(state, goal, history, provider_name="typesafe", include_screenshot=False, on_request=None):
    """Choose one operation and its compatible observed target in one request."""
    provider = decision_provider(provider_name)
    if not provider["key"]:
        raise ValueError(f"{provider['key_name']} is required for decision requests.")
    if include_screenshot and provider["name"] != "typesafe":
        raise ValueError("Screenshot model input is currently supported only by Jev AI.")
    
    elements, targets, controls = action_space(state["actions"])
    operations = {
        **{name: TARGET_OPERATION_CRITERIA[name] for name in targets},
        **{name: control["label"] for name, control in controls.items()},
        **TERMINAL_OPERATION_CRITERIA,
    }
    # Append shared guidance only when QUESTION is configured.
    operation_rules = [rule for rule in (QUESTION_OPERATION, QUESTION) if rule]
    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": {"goal": goal, "rules": operation_rules},
        }
    }
    for operation, candidates in targets.items():
        # Each dynamic target head receives guidance for its operation.
        target_rules = [rule for rule in (TARGET_QUESTIONS[operation], QUESTION) if rule]
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": target_rules},
        }
    body = {
        "model": provider["model"],
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    if include_screenshot:
        _add_screenshot(body, state)
    feature_headers = {"TypeSafe-Danger-Zone-Images": "true"} if include_screenshot else {}
    if on_request:
        on_request({"url": provider["url"], "headers": feature_headers, "body": body})
    started = time.perf_counter()
    if include_screenshot:
        result = post_json(
            provider["url"],
            provider["key"],
            body,
            feature_headers,
        )
    else:
        result = post_json(provider["url"], provider["key"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": _inspectable_request(body),
    }


def field_context(goal, action, page, history):
    """Build the complete, stable helper input for one text field."""
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context):
    """Generate and validate text for a selected editable field."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": QUESTION_TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        print("[ERROR LOG] : field_text output : " , result["choices"][0]["message"]["content"])
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
