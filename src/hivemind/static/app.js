const state = {
  setupComplete: false,
  authMode: null,
  me: null,
  config: null,
  agents: [],
  credentials: [],
  oauthProviders: [],
  leases: [],
  tasks: [],
  schedules: [],
  heartbeats: [],
  auditEvents: [],
  selectedCredentialTemplate: "github_oauth_app",
};

const credentialTemplates = {
  github_oauth_app: {
    label: "GitHub OAuth App",
    provider: "github",
    summary: "Capture the public client ID plus a host-side ref for the client secret.",
    note: "Use this when Hivemind needs to broker OAuth exchange or refresh actions for a GitHub OAuth app.",
    defaults: {
      name: "GitHub OAuth App",
      allowedActions: "exchange_oauth_code,refresh_oauth_token",
      maxTtlSeconds: 300,
      requireIntent: true,
    },
    renderFields() {
      return `
        <div class="two-col">
          <label>provider<input value="github" readonly /></label>
          <label>client id<input name="client_id" placeholder="Iv1.0123456789abcdef" autocomplete="off" required /></label>
        </div>
        <label>client secret ref<input name="client_secret_ref" value="file:///var/lib/hivemind/github-oauth-app.ref" autocomplete="off" required /></label>
        <p class="field-hint">Keep the client secret on the host. Point Hivemind at an <code>env://</code> or <code>file://</code> ref instead of pasting the value here.</p>
      `;
    },
    buildPayload(form) {
      return {
        provider: "github",
        secret_ref: form.elements.client_secret_ref.value.trim(),
        metadata: {
          credential_kind: "github_oauth_app",
          client_id: form.elements.client_id.value.trim(),
        },
      };
    },
  },
  github_app: {
    label: "GitHub App",
    provider: "github",
    summary: "Store app identifiers in metadata and keep the PEM private key behind a host-side ref.",
    note: "Use this for GitHub App installation flows where Hivemind needs the app ID, installation ID, and private key reference.",
    defaults: {
      name: "GitHub App Installation",
      allowedActions: "issue_installation_token,read_repo",
      maxTtlSeconds: 300,
      requireIntent: true,
    },
    renderFields() {
      return `
        <div class="two-col">
          <label>provider<input value="github" readonly /></label>
          <label>app id<input name="app_id" placeholder="123456" inputmode="numeric" autocomplete="off" required /></label>
        </div>
        <div class="two-col">
          <label>installation id<input name="installation_id" placeholder="987654321" inputmode="numeric" autocomplete="off" required /></label>
          <label>private key ref<input name="private_key_ref" value="file:///var/lib/hivemind/github-app.pem" autocomplete="off" required /></label>
        </div>
        <p class="field-hint">Keep the PEM on disk or in environment-backed secret storage. Hivemind records only the reference and redacts it in public views.</p>
      `;
    },
    buildPayload(form) {
      return {
        provider: "github",
        secret_ref: form.elements.private_key_ref.value.trim(),
        metadata: {
          credential_kind: "github_app",
          app_id: form.elements.app_id.value.trim(),
          installation_id: form.elements.installation_id.value.trim(),
        },
      };
    },
  },
  generic_reference: {
    label: "Generic Ref",
    provider: "custom",
    summary: "Add a plain provider name plus a single host-side secret reference.",
    note: "Use this for providers that do not need a specialized guided form yet.",
    defaults: {
      name: "Generic Credential Ref",
      allowedActions: "read_repo",
      maxTtlSeconds: 300,
      requireIntent: true,
    },
    renderFields() {
      return `
        <div class="two-col">
          <label>provider<input name="provider" value="custom" autocomplete="off" required /></label>
          <label>primary ref<input name="secret_ref" value="vault://provider/default-token-ref" autocomplete="off" required /></label>
        </div>
        <p class="field-hint">Use <code>env://</code>, <code>file://</code>, <code>vault://</code>, or <code>oauth://</code> depending on where the secret material lives.</p>
      `;
    },
    buildPayload(form) {
      return {
        provider: form.elements.provider.value.trim(),
        secret_ref: form.elements.secret_ref.value.trim(),
        metadata: {
          credential_kind: "generic_reference",
        },
      };
    },
  },
};

const credentialKindLabels = {
  github_oauth_app: "GitHub OAuth App",
  github_app: "GitHub App",
  generic_reference: "Generic Ref",
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

function credentialKindLabel(kind) {
  return credentialKindLabels[kind] || "Credential Ref";
}

function credentialTypeLabel(credential) {
  return credential.metadata?.auth_type === "oauth" ? "OAuth Broker Credential" : credentialKindLabel(credential.metadata?.credential_kind);
}

function credentialAgentScope(credential) {
  return credential.policy.allowed_agents.length
    ? credential.policy.allowed_agents.map((agentId) => agentName(agentId)).join(", ")
    : "none";
}

function credentialDetailRows(credential) {
  const metadata = credential.metadata || {};
  const rows = [
    `ID: ${escapeHtml(credential.id)}`,
    `Type: ${escapeHtml(credentialTypeLabel(credential))}`,
    `Provider: ${escapeHtml(credential.provider)}`,
    `Primary ref: ${escapeHtml(credential.secret_ref_preview)}`,
  ];
  if (metadata.client_id) {
    rows.push(`Client ID: ${escapeHtml(metadata.client_id)}`);
  }
  if (metadata.app_id) {
    rows.push(`App ID: ${escapeHtml(metadata.app_id)}`);
  }
  if (metadata.installation_id) {
    rows.push(`Installation ID: ${escapeHtml(metadata.installation_id)}`);
  }
  if (Array.isArray(metadata.oauth_scopes) && metadata.oauth_scopes.length) {
    rows.push(`OAuth scopes: ${escapeHtml(metadata.oauth_scopes.join(" "))}`);
  }
  if (metadata.oauth_connected_at) {
    rows.push(`OAuth connected: ${escapeHtml(metadata.oauth_connected_at)}`);
  }
  if (metadata.oauth_token_expires_at) {
    rows.push(`Token expiry: ${escapeHtml(metadata.oauth_token_expires_at)}`);
  }
  rows.push(`Allowed agents: ${escapeHtml(credentialAgentScope(credential))}`);
  rows.push(`Max TTL: ${escapeHtml(credential.policy.max_ttl_seconds)}s`);
  rows.push(`Intent review: ${escapeHtml(credential.policy.require_intent ? "required" : "optional")}`);
  return rows.join("<br>");
}

function renderCredentialTemplatePicker() {
  $("#credential-template-picker").innerHTML = Object.entries(credentialTemplates)
    .map(
      ([templateId, template]) => `
        <button
          type="button"
          class="template-card${state.selectedCredentialTemplate === templateId ? " active" : ""}"
          data-credential-template="${templateId}"
          aria-pressed="${state.selectedCredentialTemplate === templateId}"
        >
          <strong>${escapeHtml(template.label)}</strong>
          <span>${escapeHtml(template.summary)}</span>
        </button>`,
    )
    .join("");
}

function applyCredentialTemplate(reset = false) {
  const template = credentialTemplates[state.selectedCredentialTemplate];
  const form = $("#credential-form");
  renderCredentialTemplatePicker();
  $("#credential-template-note").innerHTML = `
    <p class="eyebrow">template note</p>
    <p>${escapeHtml(template.note)}</p>
  `;
  $("#credential-template-fields").innerHTML = template.renderFields();
  if (!reset) return;
  form.reset();
  form.elements.name.value = template.defaults.name;
  form.elements.allowed_actions.value = template.defaults.allowedActions;
  form.elements.max_ttl_seconds.value = String(template.defaults.maxTtlSeconds);
  form.elements.require_intent.checked = template.defaults.requireIntent;
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
  const authMode = state.setupComplete ? "login" : "setup";
  const authForm = $("#auth-form");
  const usernameInput = authForm.elements.username;
  const passwordInput = authForm.elements.password;

  if (state.authMode !== authMode) {
    authForm.reset();
    state.authMode = authMode;
  }

  $("#auth-view").hidden = Boolean(state.me);
  $("#app-view").hidden = !state.me;
  $("#logout-button").hidden = !state.me;
  $("#refresh-button").hidden = !state.me;
  usernameInput.placeholder = state.setupComplete ? "username" : "local-admin";
  passwordInput.placeholder = state.setupComplete ? "password" : "create admin password";
  passwordInput.autocomplete = state.setupComplete ? "current-password" : "new-password";
  $("#auth-title").textContent = state.setupComplete ? "Log in" : "Set up admin";
  $("#auth-mode").textContent = state.setupComplete ? "Use your local Hivemind account." : "First local user becomes admin.";
  $("#session-line").textContent = state.me ? `${state.me.username} / ${state.me.role}` : "Not signed in";
  renderNavigation();
}

function renderSelectors() {
  for (const selector of [
    '#lease-form select[name="agent_id"]',
    '#credential-form select[name="allowed_agents"]',
    '#codex-oauth-form select[name="allowed_agents"]',
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
      const metadata = credential.metadata || {};
      const pills = [
        metadata.auth_type === "oauth" ? "oauth" : credentialTypeLabel(credential),
        credential.provider,
        `TTL ${credential.policy.max_ttl_seconds}s`,
        credential.policy.require_intent ? "intent required" : "intent optional",
        ...credential.policy.allowed_actions,
      ];
      if (credential.metadata?.auth_type === "oauth") {
        pills.push(credential.metadata?.oauth_refreshable ? "refreshable" : "access-only");
      }
      return item(credential.name, credentialDetailRows(credential), pills);
    })
    .join("") || '<p class="meta">No credentials yet.</p>';
  $("#credential-page-count").textContent = state.credentials.length;
}

function renderOAuthProviders() {
  const provider = state.oauthProviders.find((item) => item.id === "codex");
  const stateNode = $("#oauth-provider-state");
  const detailNode = $("#oauth-provider-detail");
  const button = $("#codex-oauth-button");
  if (!provider) {
    stateNode.textContent = "missing";
    stateNode.dataset.state = "error";
    detailNode.textContent = "Codex OAuth profile is unavailable in this build.";
    button.disabled = true;
    return;
  }
  stateNode.textContent = provider.available ? "ready" : "blocked";
  stateNode.dataset.state = provider.available ? "ready" : "error";
  button.disabled = !provider.available;
  button.textContent = provider.available ? "connect via oauth" : "oauth unavailable";
  detailNode.textContent = provider.available
    ? `Scopes: ${provider.scopes.join(" ")}`
    : provider.reason;
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
    .map((schedule) => {
      const actions = `
        <div class="button-row">
          <button data-schedule-enabled="${escapeHtml(schedule.id)}" data-enabled="${schedule.enabled ? "false" : "true"}" type="button">
            ${schedule.enabled ? "Pause" : "Enable"}
          </button>
        </div>`;
      return item(
        schedule.name,
        `Every ${escapeHtml(schedule.interval_seconds)}s<br>Next run: ${escapeHtml(schedule.next_run_at)}<br>Last run: ${escapeHtml(schedule.last_run_at || "never")}<br>Task: ${escapeHtml(schedule.task_title)}`,
        [schedule.enabled ? "enabled" : "paused"],
        actions,
      );
    })
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
  renderOAuthProviders();
  renderAgents();
  renderCredentials();
  renderLeases();
  renderTasks();
  renderSchedules();
  renderAudit();
  renderCredentialAudit();
}

function consumeOAuthStatus() {
  const params = new URLSearchParams(window.location.search);
  const status = params.get("oauth");
  if (!status) return;
  const detail = params.get("detail") || (status === "connected" ? "OAuth connected." : "OAuth flow failed.");
  toast(detail);
  params.delete("oauth");
  params.delete("detail");
  const nextQuery = params.toString();
  const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ""}${window.location.hash}`;
  window.history.replaceState({}, "", nextUrl);
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
  const [config, agents, credentials, oauthProviders, leases, tasks, schedules, heartbeats, auditEvents] = await Promise.all([
    api("/config"),
    api("/agents"),
    api("/credentials"),
    api("/oauth/providers"),
    api("/credential-leases"),
    api("/tasks"),
    api("/schedules"),
    api("/heartbeats"),
    api("/audit-events"),
  ]);
  Object.assign(state, { config, agents, credentials, oauthProviders, leases, tasks, schedules, heartbeats, auditEvents });
  render();
  consumeOAuthStatus();
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = readForm(form);
  const path = state.setupComplete ? "/auth/login" : "/auth/setup";
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    form.reset();
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

$("#credential-template-picker").addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const button = target.closest("[data-credential-template]");
  if (!(button instanceof HTMLButtonElement)) return;
  if (!button.dataset.credentialTemplate) return;
  state.selectedCredentialTemplate = button.dataset.credentialTemplate;
  applyCredentialTemplate(true);
  renderSelectors();
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
  const template = credentialTemplates[state.selectedCredentialTemplate];
  const payload = readForm(form);
  const templatePayload = template.buildPayload(form);
  payload.name = payload.name.trim();
  payload.provider = templatePayload.provider;
  payload.secret_ref = templatePayload.secret_ref;
  payload.allowed_agents = selectedValues(form.elements.allowed_agents);
  payload.allowed_actions = splitCsv(payload.allowed_actions);
  payload.max_ttl_seconds = Number(payload.max_ttl_seconds);
  payload.require_intent = form.elements.require_intent.checked;
  payload.metadata = templatePayload.metadata;
  try {
    await api("/credentials", { method: "POST", body: JSON.stringify(payload) });
    applyCredentialTemplate(true);
    renderSelectors();
    await refresh();
    toast("Credential policy created.");
  } catch (error) {
    toast(error.message);
  }
});

$("#codex-oauth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = readForm(form);
  payload.provider = "codex";
  payload.allowed_agents = selectedValues(form.elements.allowed_agents);
  payload.allowed_actions = splitCsv(payload.allowed_actions);
  payload.max_ttl_seconds = Number(payload.max_ttl_seconds);
  payload.require_intent = form.elements.require_intent.checked;
  payload.metadata = {};
  try {
    const result = await api("/oauth/credentials/start", { method: "POST", body: JSON.stringify(payload) });
    window.location.assign(result.authorize_url);
  } catch (error) {
    toast(error.message);
  }
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

$("#schedules-list").addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  if (!target.dataset.scheduleEnabled) return;
  await api(`/schedules/${target.dataset.scheduleEnabled}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled: target.dataset.enabled === "true" }),
  });
  await refresh();
  toast(target.dataset.enabled === "true" ? "Schedule enabled." : "Schedule paused.");
});

$("#run-due-button").addEventListener("click", async () => {
  const result = await api("/schedules/run-due", { method: "POST" });
  await refresh();
  toast(`${result.created_tasks.length} due schedule(s) ran.`);
});

applyCredentialTemplate(true);
renderNavigation();
refresh().catch((error) => toast(error.message));
