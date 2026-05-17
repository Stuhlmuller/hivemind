const state = {
  setupComplete: false,
  me: null,
  config: null,
  agents: [],
  credentials: [],
  leases: [],
  tasks: [],
  schedules: [],
  heartbeats: [],
  auditEvents: [],
};

const ROUTES = {
  overview: "/",
  credentials: "/control/credentials",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `Request failed: ${response.status}`);
  }
  return body;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("visible"), 2600);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function readForm(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function selectedValues(select) {
  return Array.from(select.selectedOptions).map((option) => option.value);
}

function splitCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function item(title, meta, pills = [], actions = "") {
  const pillMarkup = pills.length
    ? `<div class="pill-row">${pills.map((pill) => `<span class="pill">${escapeHtml(pill)}</span>`).join("")}</div>`
    : "";
  return `<article class="item"><strong>${escapeHtml(title)}</strong><div class="meta">${meta}</div>${pillMarkup}${actions}</article>`;
}

function optionList(items, labelKey = "name", includeEmpty = false) {
  const empty = includeEmpty ? '<option value="">None</option>' : "";
  return empty + items.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item[labelKey])}</option>`).join("");
}

function currentPage() {
  return window.location.pathname.startsWith(ROUTES.credentials) ? "credentials" : "overview";
}

function credentialName(credentialId) {
  const credential = state.credentials.find((item) => item.id === credentialId);
  return credential ? credential.name : credentialId;
}

function agentName(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  return agent ? agent.name : agentId;
}

function renderNavigation() {
  const page = currentPage();
  $("#overview-page").hidden = page !== "overview";
  $("#credentials-page").hidden = page !== "credentials";
  $("#surface-line").textContent =
    page === "credentials" ? "credential broker / policies, leases, audit" : "runtime overview / agents, tasks, schedules";
  for (const link of $$("[data-page-link]")) {
    const active = link.dataset.pageLink === page;
    link.classList.toggle("active", active);
    if (active) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
}

function renderAuth() {
  $("#auth-view").hidden = Boolean(state.me);
  $("#app-view").hidden = !state.me;
  $("#logout-button").hidden = !state.me;
  $("#refresh-button").hidden = !state.me;
  $("#auth-title").textContent = state.setupComplete ? "Log in" : "Set up admin";
  $("#auth-mode").textContent = state.setupComplete ? "Use your local Hivemind account." : "First local user becomes admin.";
  $("#session-line").textContent = state.me ? `${state.me.username} / ${state.me.role}` : "Not signed in";
  renderNavigation();
}

function renderSelectors() {
  for (const selector of [
    '#lease-form select[name="agent_id"]',
    '#credential-form select[name="allowed_agents"]',
    '#task-form select[name="assigned_agent_id"]',
    '#schedule-form select[name="assigned_agent_id"]',
  ]) {
    $(selector).innerHTML = optionList(state.agents);
  }
  for (const selector of [
    '#lease-form select[name="credential_id"]',
    '#task-form select[name="credential_id"]',
    '#schedule-form select[name="credential_id"]',
  ]) {
    $(selector).innerHTML = optionList(state.credentials, "name", selector !== '#lease-form select[name="credential_id"]');
  }
}

function renderAgents() {
  $("#agent-count").textContent = state.agents.length;
  $("#agents-list").innerHTML = state.agents
    .map((agent) =>
      item(
        agent.name,
        `${escapeHtml(agent.role)}<br>ID: ${escapeHtml(agent.id)}`,
        [agent.status, agent.provider, agent.model],
      ),
    )
    .join("") || '<p class="meta">No agents yet.</p>';
}

function renderCredentials() {
  $("#credential-count").textContent = state.credentials.length;
  $("#credentials-list").innerHTML = state.credentials
    .map((credential) => {
      const allowedAgents = credential.policy.allowed_agents.length
        ? credential.policy.allowed_agents.map((agentId) => agentName(agentId)).join(", ")
        : "none";
      return item(
        credential.name,
        `ID: ${escapeHtml(credential.id)}<br>Provider: ${escapeHtml(credential.provider)}<br>Secret ref: ${escapeHtml(credential.secret_ref_preview)}<br>Allowed agents: ${escapeHtml(allowedAgents)}<br>Max TTL: ${escapeHtml(credential.policy.max_ttl_seconds)}s<br>Intent review: ${escapeHtml(credential.policy.require_intent ? "required" : "optional")}`,
        [credential.provider, ...credential.policy.allowed_actions],
      );
    })
    .join("") || '<p class="meta">No credentials yet.</p>';
  $("#credential-page-count").textContent = state.credentials.length;
}

function renderLeases() {
  $("#lease-count").textContent = state.leases.length;
  $("#leases-list").innerHTML = state.leases
    .map((lease) =>
      item(
        lease.id,
        `Agent: ${escapeHtml(agentName(lease.agent_id))}<br>Credential: ${escapeHtml(credentialName(lease.credential_id))}<br>Issued: ${escapeHtml(lease.issued_at)}<br>Expires: ${escapeHtml(lease.expires_at)}`,
        [lease.status, lease.action, lease.token_preview],
      ),
    )
    .join("") || '<p class="meta">No leases yet.</p>';
  $("#credential-active-lease-count").textContent = state.leases.filter((lease) => lease.status === "active").length;
  $("#credential-expired-lease-count").textContent = state.leases.filter((lease) => lease.status === "expired").length;
}

function renderTasks() {
  $("#task-count").textContent = state.tasks.length;
  $("#tasks-list").innerHTML = state.tasks
    .map((task) => {
      const actions = `
        <div class="button-row">
          <button data-task-status="${escapeHtml(task.id)}" data-status="running" type="button">Start</button>
          <button data-task-status="${escapeHtml(task.id)}" data-status="done" type="button">Done</button>
          <button data-task-heartbeat="${escapeHtml(task.id)}" type="button">Heartbeat</button>
        </div>`;
      return item(
        task.title,
        `${escapeHtml(task.description)}<br>Status: ${escapeHtml(task.status)}<br>Agent: ${escapeHtml(task.assigned_agent_id || "unassigned")}<br>Next heartbeat: ${escapeHtml(task.next_heartbeat_at || "none")}`,
        [task.priority],
        actions,
      );
    })
    .join("") || '<p class="meta">No tasks yet.</p>';
}

function renderSchedules() {
  $("#schedule-count").textContent = state.schedules.length;
  $("#schedules-list").innerHTML = state.schedules
    .map((schedule) =>
      item(
        schedule.name,
        `Every ${escapeHtml(schedule.interval_seconds)}s<br>Next run: ${escapeHtml(schedule.next_run_at)}<br>Task: ${escapeHtml(schedule.task_title)}`,
        [schedule.enabled ? "enabled" : "paused"],
      ),
    )
    .join("") || '<p class="meta">No schedules yet.</p>';
}

function renderAudit() {
  $("#audit-count").textContent = state.auditEvents.length;
  $("#audit-list").innerHTML = state.auditEvents
    .map(
      (event) =>
        `<article class="event"><strong>${escapeHtml(event.type)}</strong><div class="meta">${escapeHtml(event.decision)}: ${escapeHtml(event.reason)}<br>Actor: ${escapeHtml(event.actor_id)} -> Target: ${escapeHtml(event.target_id)}<br>${escapeHtml(event.created_at)}</div></article>`,
    )
    .join("") || '<p class="meta">No broker activity yet.</p>';
}

function renderCredentialAudit() {
  const events = state.auditEvents.filter((event) => event.type.startsWith("credential."));
  $("#credential-denied-count").textContent = events.filter((event) => event.decision === "denied").length;
  $("#credential-audit-list").innerHTML = events
    .map((event) => {
      const action = event.metadata?.action ? `<br>Action: ${escapeHtml(event.metadata.action)}` : "";
      const ttl = Number.isFinite(event.metadata?.ttl_seconds) ? `<br>TTL: ${escapeHtml(event.metadata.ttl_seconds)}s` : "";
      return `<article class="event"><strong>${escapeHtml(event.type)}</strong><div class="meta">${escapeHtml(event.decision)}: ${escapeHtml(event.reason)}<br>Actor: ${escapeHtml(event.actor_id)} -> Target: ${escapeHtml(event.target_id)}${action}${ttl}<br>${escapeHtml(event.created_at)}</div></article>`;
    })
    .join("") || '<p class="meta">No credential audit events yet.</p>';
}

function renderConfig() {
  const reviewer = state.config?.intent_reviewer;
  $("#reviewer-config").textContent = reviewer ? `${reviewer.provider} / ${reviewer.model}` : "No reviewer configured";
}

function render() {
  renderAuth();
  if (!state.me) return;
  renderConfig();
  renderSelectors();
  renderAgents();
  renderCredentials();
  renderLeases();
  renderTasks();
  renderSchedules();
  renderAudit();
  renderCredentialAudit();
}

async function loadSetupState() {
  const setup = await api("/setup-state");
  state.setupComplete = setup.setup_complete;
}

async function refresh() {
  await loadSetupState();
  try {
    state.me = await api("/me");
  } catch {
    state.me = null;
    render();
    return;
  }
  const [config, agents, credentials, leases, tasks, schedules, heartbeats, auditEvents] = await Promise.all([
    api("/config"),
    api("/agents"),
    api("/credentials"),
    api("/credential-leases"),
    api("/tasks"),
    api("/schedules"),
    api("/heartbeats"),
    api("/audit-events"),
  ]);
  Object.assign(state, { config, agents, credentials, leases, tasks, schedules, heartbeats, auditEvents });
  render();
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm(event.currentTarget);
  const path = state.setupComplete ? "/auth/login" : "/auth/setup";
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    await refresh();
    toast(state.setupComplete ? "Signed in." : "Admin created.");
  } catch (error) {
    toast(error.message);
  }
});

$("#logout-button").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" });
  state.me = null;
  await refresh();
  toast("Signed out.");
});

$("#refresh-button").addEventListener("click", async () => {
  await refresh();
  toast("Runtime refreshed.");
});

for (const link of $$("[data-page-link]")) {
  link.addEventListener("click", (event) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    const path = link.getAttribute("href");
    if (path && path !== window.location.pathname) {
      window.history.pushState({}, "", path);
    }
    renderNavigation();
  });
}

window.addEventListener("popstate", () => {
  renderNavigation();
});

$("#spawn-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await api("/agents", { method: "POST", body: JSON.stringify(readForm(event.currentTarget)) });
  await refresh();
  toast("Agent joined the swarm.");
});

$("#credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = readForm(form);
  payload.allowed_agents = selectedValues(form.elements.allowed_agents);
  payload.allowed_actions = splitCsv(payload.allowed_actions);
  payload.max_ttl_seconds = Number(payload.max_ttl_seconds);
  payload.require_intent = form.elements.require_intent.checked;
  payload.metadata = {};
  await api("/credentials", { method: "POST", body: JSON.stringify(payload) });
  await refresh();
  toast("Credential policy created.");
});

$("#lease-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm(event.currentTarget);
  payload.ttl_seconds = Number(payload.ttl_seconds);
  try {
    const lease = await api("/credential-leases", { method: "POST", body: JSON.stringify(payload) });
    $('#action-form input[name="lease_token"]').value = lease.lease_token;
    $('#action-form input[name="action"]').value = lease.action;
    await refresh();
    toast("Lease issued.");
  } catch (error) {
    await refresh();
    toast(error.message);
  }
});

$("#action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm(event.currentTarget);
  try {
    payload.payload = JSON.parse(payload.payload || "{}");
    const result = await api("/credential-actions", { method: "POST", body: JSON.stringify(payload) });
    $("#action-result").textContent = JSON.stringify(result, null, 2);
    await refresh();
    toast("Broker accepted the action.");
  } catch (error) {
    $("#action-result").textContent = error.message;
    await refresh();
    toast(error.message);
  }
});

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm(event.currentTarget);
  payload.heartbeat_seconds = payload.heartbeat_seconds ? Number(payload.heartbeat_seconds) : null;
  await api("/tasks", { method: "POST", body: JSON.stringify(payload) });
  await refresh();
  toast("Task created.");
});

$("#tasks-list").addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  if (target.dataset.taskStatus) {
    await api(`/tasks/${target.dataset.taskStatus}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status: target.dataset.status }),
    });
    await refresh();
    toast("Task updated.");
  }
  if (target.dataset.taskHeartbeat) {
    await api(`/tasks/${target.dataset.taskHeartbeat}/heartbeats`, {
      method: "POST",
      body: JSON.stringify({ note: "manual heartbeat from console" }),
    });
    await refresh();
    toast("Heartbeat recorded.");
  }
});

$("#schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = readForm(form);
  payload.interval_seconds = Number(payload.interval_seconds);
  payload.enabled = form.elements.enabled.checked;
  await api("/schedules", { method: "POST", body: JSON.stringify(payload) });
  await refresh();
  toast("Schedule created.");
});

$("#run-due-button").addEventListener("click", async () => {
  const result = await api("/schedules/run-due", { method: "POST" });
  await refresh();
  toast(`${result.created_tasks.length} due schedule(s) ran.`);
});

renderNavigation();
refresh().catch((error) => toast(error.message));
