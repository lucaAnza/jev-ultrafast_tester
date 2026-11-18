"""Persist model requests and answers for one test run."""

import json


def log_request(path, request, number):
    """Write the request before transmission without an authorization header."""
    if path is None:
        return
    with path.open("a", encoding="utf-8") as log:
        log.write(f"Decision {number} request:\n")
        json.dump(request, log, ensure_ascii=False, indent=2)
        log.write("\n\n")


def log_decision(path, decision, page, number, totals):
    """Update token totals and append one model answer to the run log."""
    usage = decision.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    input_tokens = input_tokens if isinstance(input_tokens, int) else 0
    output_tokens = output_tokens if isinstance(output_tokens, int) else 0
    totals["requests"] += 1
    totals["input_tokens"] += input_tokens
    totals["output_tokens"] += output_tokens
    print(f"Decision request {number} - input tokens: {input_tokens}, output tokens: {output_tokens}", flush=True)
    if path is None:
        return
    action = next((item for item in page["actions"] if item["id"] == decision["choice"]), None)
    label = " ".join((action["label"] if action else decision["choice"]).split())
    output = {key: value for key, value in decision.items() if key != "request"}
    with path.open("a", encoding="utf-8") as log:
        log.write(f"Decision {number} - {label}:\n")
        log.write("Index | Input tokens | Output tokens\n")
        log.write(f"{number} | {input_tokens} | {output_tokens}\n")
        log.write(f"{number} | {totals['input_tokens']} | {totals['output_tokens']}\n\n")
        log.write(f"Accumulated cost: ${totals['input_tokens'] / 1_000_000 * 0.042:.8f}\n\n")
        json.dump(output, log, ensure_ascii=False, indent=2)
        log.write("\n\n")
