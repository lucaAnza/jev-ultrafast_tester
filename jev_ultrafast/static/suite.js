const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let suite = null;
let runningAll = false;

const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character],
  );

async function post(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  return data;
}

async function readSuite() {
  const response = await fetch("/api/suite");
  if (!response.ok) throw Error("Could not read the test suite");
  suite = await response.json();
  renderSuite();
}

function statusBadge(test) {
  const labels = {
    pending: "Pending",
    running: "Running…",
    done: "✓ Done",
    blocked: "✕ Blocked",
    error: "✕ Error",
  };
  return `<span class="test-status ${escape(test.status)}">${labels[test.status] || escape(test.status)}</span>`;
}

function renderSuite() {
  const configured = Boolean(suite?.configured);
  $("suite-setup").hidden = configured;
  $("suite-results").hidden = !configured;
  if (!configured) return;

  const completed = suite.tests.filter((test) => ["done", "blocked", "error"].includes(test.status)).length;
  $("suite-summary").textContent = `${suite.tests.length} tests · ${completed} completed`;
  $("suite-target").textContent = `${suite.url} · log: ${suite.log_file}`;
  $("test-all").disabled = Boolean(suite.active_id) || runningAll;
  $("suite-replace").disabled = Boolean(suite.active_id) || runningAll;

  $("test-list").innerHTML = suite.tests
    .map(
      (test) => `
        <article class="test-card ${escape(test.status)}">
          <div class="test-main">
            <div class="test-id">${escape(test.id)}</div>
            <h3>${escape(test.description)}</h3>
            ${test.error ? `<p class="test-error">${escape(test.error)}</p>` : ""}
            ${test.choices.length ? `
              <div class="live-choices" aria-live="polite">
                ${test.choices.slice(-5).map((choice) => `
                  <div class="live-choice">
                    <span>${String(choice.step).padStart(2, "0")}</span>
                    <b>${escape(choice.operation)}</b>
                    <p>${escape(choice.target)}</p>
                    <time>${choice.elapsed_ms} ms</time>
                  </div>`).join("")}
              </div>` : ""}
          </div>
          <div class="test-meta">
            ${statusBadge(test)}
            ${test.elapsed_ms ? `<span>${(test.elapsed_ms / 1000).toFixed(2)} s</span>` : ""}
          </div>
          <div class="test-actions">
            <button type="button" data-actions="${escape(test.id)}" ${["done", "blocked", "error"].includes(test.status) ? "" : "disabled"}>View actions</button>
            <button type="button" class="primary" data-run="${escape(test.id)}" ${suite.active_id || runningAll ? "disabled" : ""}>
              ${["done", "blocked", "error"].includes(test.status) ? "Run again" : "Run test"}
            </button>
          </div>
        </article>`,
    )
    .join("");
}

async function runTest(testId) {
  const test = suite.tests.find((item) => item.id === testId);
  if (!test) return;
  test.status = "running";
  test.error = null;
  test.actions = [];
  suite.active_id = testId;
  renderSuite();

  try {
    let agent = await post("suite/start", { test_id: testId });
    for (let step = 0; step < agent.max_steps * 2; step += 1) {
      if (["done", "blocked"].includes(agent.status)) break;
      agent = await post("tick");
      const currentTest = suite.tests.find((item) => item.id === testId);
      currentTest.elapsed_ms = agent.elapsed_ms;
      currentTest.choices = (agent.decisions || []).map((decision, index) => ({
        step: index + 1,
        operation: decision.operation,
        target: decision.action_label || decision.choice,
        elapsed_ms: decision.elapsed_ms,
      }));
      renderSuite();
    }
    if (!["done", "blocked"].includes(agent.status)) throw Error("The test reached its execution limit");
  } catch (error) {
    await post("suite/fail", { test_id: testId, error: error.message });
  } finally {
    await readSuite();
  }
}

function showActions(testId) {
  const test = suite.tests.find((item) => item.id === testId);
  if (!test) return;
  $("actions-title").textContent = `${test.id} · ${test.actions.length} actions`;
  $("actions-list").innerHTML = test.actions.length
    ? test.actions
        .map((action) => {
          const detail = action.text || action.value || (action.delta != null ? `delta ${action.delta}` : "");
          return `<div class="action-row"><span>${String(action.step).padStart(2, "0")}</span><div><b>${escape(action.command)}</b><p>${escape(action.action)}</p>${detail ? `<small>${escape(detail)}</small>` : ""}</div><time>${action.sent_ms ?? 0} ms</time></div>`;
        })
        .join("")
    : '<p class="muted">This result contains no browser mutations.</p>';
  $("actions-dialog").showModal();
}

$("suite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("suite-load");
  button.disabled = true;
  try {
    const file = $("suite-file").files[0];
    const data = JSON.parse(await file.text());
    suite = await post("suite/load", {
      url: $("suite-url").value,
      decision_provider: $("suite-provider").value,
      suite: data,
    });
    renderSuite();
  } catch (error) {
    window.alert(error instanceof SyntaxError ? "The selected file is not valid JSON." : error.message);
  } finally {
    button.disabled = false;
  }
});

$("test-list").addEventListener("click", async (event) => {
  const runId = event.target.closest("[data-run]")?.dataset.run;
  const actionsId = event.target.closest("[data-actions]")?.dataset.actions;
  if (runId) await runTest(runId);
  if (actionsId) showActions(actionsId);
});

$("test-all").addEventListener("click", async () => {
  runningAll = true;
  renderSuite();
  try {
    const testIds = suite.tests.map((test) => test.id);
    for (const testId of testIds) await runTest(testId);
  } finally {
    runningAll = false;
    renderSuite();
  }
});

$("suite-replace").addEventListener("click", async () => {
  suite = await post("suite/clear");
  $("suite-form").reset();
  renderSuite();
});
$("actions-close").addEventListener("click", () => $("actions-dialog").close());

readSuite().catch((error) => {
  $("suite-error").textContent = error.message;
  $("suite-error").hidden = false;
});
