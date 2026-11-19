"""Validated in-memory state for an end-to-end test suite."""

MAX_TESTS = 100
MAX_ID_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 2000


class TestSuite:
    def __init__(self):
        self.clear()

    def clear(self):
        self.url = None
        self.options = {}
        self.tests = []
        self.active_id = None

    def load(self, url, data, options):
        """Validate and store a new suite."""
        tests = validate_suite(data)
        self.url = url
        self.options = dict(options)
        self.tests = [
            {
                **test,
                "status": "pending",
                "elapsed_ms": 0,
                "actions": [],
                "error": None,
            }
            for test in tests
        ]
        self.active_id = None
        return self.snapshot()

    def start(self, test_id):
        """Mark one stored test as running and return it."""
        test = self._find(test_id)
        if self.active_id:
            raise ValueError("Another suite test is already running")
        test.update(status="running", elapsed_ms=0, actions=[], error=None)
        self.active_id = test_id
        return test

    def finish(self, agent_state):
        """Save a terminal agent result for the active test."""
        if not self.active_id or agent_state.get("status") not in {"done", "blocked"}:
            return
        test = self._find(self.active_id)
        test.update(
            status=agent_state["status"],
            elapsed_ms=agent_state.get("elapsed_ms", 0),
            actions=list(agent_state.get("harness_actions", [])),
            error=None,
        )
        self.active_id = None

    def fail(self, test_id, message):
        """Record an execution error so the suite can continue."""
        test = self._find(test_id)
        test.update(status="error", error=str(message)[:500])
        if self.active_id == test_id:
            self.active_id = None

    def snapshot(self):
        """Return JSON-safe suite state for the interface."""
        return {
            "configured": self.url is not None,
            "url": self.url,
            "options": dict(self.options),
            "tests": [{**test, "actions": list(test["actions"])} for test in self.tests],
            "active_id": self.active_id,
        }

    def _find(self, test_id):
        if not isinstance(test_id, str):
            raise ValueError("Choose a valid test")
        for test in self.tests:
            if test["id"] == test_id:
                return test
        raise ValueError("Test not found")


def validate_suite(data):
    """Validate the uploaded JSON and return normalized test entries."""
    if not isinstance(data, dict) or set(data) != {"tests"}:
        raise ValueError('JSON must contain exactly one "tests" field')
    if not isinstance(data["tests"], list) or not 1 <= len(data["tests"]) <= MAX_TESTS:
        raise ValueError(f'"tests" must contain between 1 and {MAX_TESTS} items')

    normalized = []
    seen_ids = set()
    for position, test in enumerate(data["tests"], start=1):
        if not isinstance(test, dict) or set(test) != {"id", "description"}:
            raise ValueError(f"Test {position} must contain only id and description")

        test_id = test["id"].strip() if isinstance(test["id"], str) else ""
        description = test["description"].strip() if isinstance(test["description"], str) else ""
        if not test_id or len(test_id) > MAX_ID_LENGTH:
            raise ValueError(f"Test {position} has an invalid id")
        if test_id in seen_ids:
            raise ValueError(f'Duplicate test id: "{test_id}"')
        if not description or len(description) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"Test {position} has an invalid description")

        seen_ids.add(test_id)
        normalized.append({"id": test_id, "description": description})
    return normalized
