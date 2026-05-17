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
  editingTaskIds: new Set(),
  selectedCredentialTemplate: "github_oauth_app",
};

const credentialTemplates = {
  github_oauth_app: {
    label: "GitHub OAuth Secret Ref",
    provider: "github",
    summary: "Store the public client ID plus a host-side ref for the client secret.",
    note: "Creates a credential policy record. OAuth exchange and refresh adapters are not implemented yet.",
    defaults: {
      name: "GitHub OAuth Secret Ref",
      allowedActions: "read_repo",
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
    note: "Creates a credential policy record. Installation-token issuance requires a broker adapter.",
    defaults: {
      name: "GitHub App Installation",
      allowedActions: "read_repo",
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
  managed_secret: {
    label: "Broker-Stored Secret",
    provider: "custom",
    summary: "Encrypt secret material at rest inside Hivemind instead of pointing at a host-side ref.",
    note: "Requires HIVEMIND_SECRETS_KEY. Public views still expose only a redacted secret:// reference, and only the broker can decrypt the stored material.",
    defaults: {
      name: "Broker Managed Secret",
      allowedActions: "read_repo",
      maxTtlSeconds: 300,
      requireIntent: true,
    },
    renderFields() {
      return `
        <div class="two-col">
          <label>provider<input name="provider" value="custom" autocomplete="off" required /></label>
          <label>storage<input value="broker-encrypted" readonly /></label>
        </div>
        <label>secret value<textarea name="secret_value" rows="4" autocomplete="off" required></textarea></label>
        <p class="field-hint">Use this when Hivemind should keep the secret locally in encrypted broker storage and return only a generated <code>secret://</code> ref from public APIs.</p>
      `;
    },
    buildPayload(form) {
      return {
        provider: form.elements.provider.value.trim(),
        secret_value: form.elements.secret_value.value,
        metadata: {
          credential_kind: "managed_secret",
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
  github_oauth_app: "GitHub OAuth Secret Ref",
  github_app: "GitHub App",
  managed_secret: "Broker-Stored Secret",
  generic_reference: "Generic Ref",
};

const ROUTES = {
  overview: "/",
  agents: "/control/agents",
  tasks: "/control/tasks",
  schedules: "/control/schedules",
  audit: "/control/audit",
  credentials: "/control/credentials",
};
const TASK_STATUSES = ["queued", "running", "blocked", "done", "failed", "cancelled"];
const TASK_TRANSITIONS = {
  queued: ["running", "blocked", "done", "failed", "cancelled"],
  running: ["blocked", "done", "failed", "cancelled"],
  blocked: ["queued", "running", "done", "failed", "cancelled"],
  done: [],
  failed: [],
  cancelled: [],
};
const CLOSED_TASK_STATUSES = new Set(["done", "failed", "cancelled"]);

const PAGE_META = {
  overview: "overview / runtime state",
  agents: "agents / registry, provider, status",
  tasks: "tasks / queue, intent, heartbeat",
  schedules: "schedules / intervals, due work",
  audit: "audit / decisions, state changes",
  credentials: "credential broker / policies, leases, audit",
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
    throw new Error(formatApiError(body, response.status));
  }
  return body;
}

function formatApiError(body, status) {
  return formatErrorValue(body.detail ?? body.message ?? body.error) || `Request failed: ${status}`;
}

function formatErrorValue(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.map(formatErrorItem).filter(Boolean).join("; ");
  }
  if (value && typeof value === "object") {
    if (typeof value.message === "string") return value.message;
    if (typeof value.error === "string") return value.error;
    return "";
  }
  return value == null ? "" : String(value);
}

function formatErrorItem(item) {
  if (typeof item === "string") return item;
  if (!item || typeof item !== "object") return String(item ?? "");
  const message = formatErrorValue(item.msg ?? item.message ?? item.detail);
  const location = Array.isArray(item.loc)
    ? item.loc
        .filter((part) => !["body", "query", "path"].includes(part))
        .map((part) => String(part))
        .join(".")
    : "";
  return location && message ? `${location}: ${message}` : message;
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

function normalizeTaskPayload(payload) {
  return {
    ...payload,
    assigned_agent_id: payload.assigned_agent_id || null,
    credential_id: payload.credential_id || null,
    heartbeat_seconds: payload.heartbeat_seconds ? Number(payload.heartbeat_seconds) : null,
  };
}

function setText(selector, value) {
  const node = $(selector);
  if (node) {
    node.textContent = String(value);
  }
}

function statusCount(items, status) {
  return items.filter((item) => item.status === status).length;
}

function isTaskStale(task) {
  if (!task.next_heartbeat_at || CLOSED_TASK_STATUSES.has(task.status)) return false;
  const nextHeartbeat = Date.parse(task.next_heartbeat_at);
  return Number.isFinite(nextHeartbeat) && nextHeartbeat <= Date.now();
}

function isScheduleDue(schedule) {
  if (!schedule.enabled || !schedule.next_run_at) return false;
  const nextRun = Date.parse(schedule.next_run_at);
  return Number.isFinite(nextRun) && nextRun <= Date.now();
}

function credentialKindLabel(kind) {
  return credentialKindLabels[kind] || "Credential Ref";
}

function credentialTypeLabel(credential) {
  return credential.metadata?.auth_type === "oauth" ? "OAuth Broker Credential" : credentialKindLabel(credential.metadata?.credential_kind);
}

function activeOAuthProvider() {
  return state.oauthProviders.find((provider) => provider.available) || state.oauthProviders[0] || null;
}

function oauthProviderLabel(provider) {
  return provider?.label || provider?.id || "OAuth provider";
}

function scheduleCatchUpPolicyLabel(policy) {
  return {
    skip_missed: "skip missed / keep cadence",
    run_once: "run once / reset cadence",
    backfill: "backfill every missed run",
  }[policy] || policy;
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
  rows.push(
    `Approval required: ${escapeHtml(
      credential.policy.approval_required_actions.length ? credential.policy.approval_required_actions.join(", ") : "none",
    )}`,
  );
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
  return optionListWithSelected(items, null, labelKey, includeEmpty);
}

function optionListWithSelected(items, selectedValue, labelKey = "name", includeEmpty = false) {
  const emptySelected = selectedValue === null || selectedValue === undefined || selectedValue === "";
  const empty = includeEmpty ? `<option value=""${emptySelected ? " selected" : ""}>None</option>` : "";
  return empty + items.map((item) => {
    const selected = selectedValue === item.id ? " selected" : "";
    return `<option value="${escapeHtml(item.id)}"${selected}>${escapeHtml(item[labelKey])}</option>`;
  }).join("");
}

function scalarOptionList(values, selectedValue) {
  return values.map((value) => {
    const selected = selectedValue === value ? " selected" : "";
    return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(value)}</option>`;
  }).join("");
}

function currentPage() {
  const pathname = window.location.pathname;
  const route = Object.entries(ROUTES).find(([, routePath]) => routePath !== "/" && pathname === routePath);
  return route ? route[0] : "overview";
}

function credentialName(credentialId) {
  const credential = state.credentials.find((item) => item.id === credentialId);
  return credential ? credential.name : credentialId;
}

function agentName(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  return agent ? agent.name : agentId;
}

function taskAssignmentLabel(task) {
  return task.assigned_agent_id ? agentName(task.assigned_agent_id) : "unassigned";
}

function taskCredentialLabel(task) {
  return task.credential_id ? credentialName(task.credential_id) : "none";
}

function taskHeartbeatLabel(task) {
  return task.heartbeat_seconds ? `${escapeHtml(task.heartbeat_seconds)}s` : "manual";
}

function taskHeartbeatState(task) {
  if (!task.heartbeat_seconds) return "manual";
  if (!task.next_heartbeat_at) return "pending";
  const nextHeartbeatAt = Date.parse(task.next_heartbeat_at);
  if (Number.isNaN(nextHeartbeatAt)) return "pending";
  return nextHeartbeatAt <= Date.now() ? "stale" : "waiting";
}

function latestHeartbeatsByTask() {
  const latest = new Map();
  for (const heartbeat of state.heartbeats) {
    if (!latest.has(heartbeat.task_id)) {
      latest.set(heartbeat.task_id, heartbeat);
    }
  }
  return latest;
}

function toggleTaskEdit(taskId) {
  if (state.editingTaskIds.has(taskId)) {
    state.editingTaskIds.delete(taskId);
  } else {
    state.editingTaskIds.add(taskId);
  }
  renderTasks();
}

function renderTaskStatusButtons(task) {
  const visibleStatuses = [task.status, ...(TASK_TRANSITIONS[task.status] || [])];
  return visibleStatuses.map((status) => {
    const current = status === task.status;
    return `<button class="status-button${current ? " is-current" : ""}" ${current ? "disabled" : ""} data-task-status="${escapeHtml(task.id)}" data-status="${escapeHtml(status)}" type="button">${escapeHtml(status)}</button>`;
  }).join("");
}

function renderTaskEditForm(task) {
  if (!state.editingTaskIds.has(task.id)) {
    return "";
  }
  return `
    <form class="task-edit-form stack" data-task-edit-form="${escapeHtml(task.id)}">
      <div class="inline-heading task-edit-header">
        <div>
          <h3>edit task</h3>
          <p>status stays on explicit transition buttons</p>
        </div>
        <code>${escapeHtml(task.id)}</code>
      </div>
      <div class="two-col">
        <label>title<input name="title" value="${escapeHtml(task.title)}" autocomplete="off" required /></label>
        <label>priority<select name="priority">${scalarOptionList(["low", "normal", "high", "urgent"], task.priority)}</select></label>
      </div>
      <div class="two-col">
        <label>agent<select name="assigned_agent_id">${optionListWithSelected(state.agents, task.assigned_agent_id, "name", true)}</select></label>
        <label>credential<select name="credential_id">${optionListWithSelected(state.credentials, task.credential_id, "name", true)}</select></label>
      </div>
      <div class="two-col">
        <label>action<input name="action" value="${escapeHtml(task.action || "")}" autocomplete="off" /></label>
        <label>heartbeat seconds<input name="heartbeat_seconds" type="number" min="30" value="${escapeHtml(task.heartbeat_seconds ?? "")}" /></label>
      </div>
      <label>description<textarea name="description" rows="3">${escapeHtml(task.description || "")}</textarea></label>
      <label>intent<textarea name="intent" rows="3">${escapeHtml(task.intent || "")}</textarea></label>
      <div class="button-row">
        <button type="submit">save edit</button>
        <button data-task-edit-toggle="${escapeHtml(task.id)}" type="button">close</button>
      </div>
    </form>`;
}

function renderTaskHealth() {
  const counts = Object.fromEntries(TASK_STATUSES.map((status) => [status, 0]));
  let staleHeartbeats = 0;
  for (const task of state.tasks) {
    counts[task.status] = (counts[task.status] || 0) + 1;
    if (taskHeartbeatState(task) === "stale") {
      staleHeartbeats += 1;
    }
  }
  const dueSchedules = state.schedules.filter((schedule) => {
    if (!schedule.enabled || !schedule.next_run_at) {
      return false;
    }
    const nextRunAt = Date.parse(schedule.next_run_at);
    return !Number.isNaN(nextRunAt) && nextRunAt <= Date.now();
  }).length;
  const taskAudits = state.auditEvents.filter((event) => event.type.startsWith("task.")).length;
  $("#running-task-count").textContent = counts.running;
  $("#blocked-task-count").textContent = counts.blocked;
  $("#due-schedule-count").textContent = dueSchedules;
  $("#stale-heartbeat-count").textContent = staleHeartbeats;
  $("#task-health").innerHTML = [
    ["queued", counts.queued],
    ["running", counts.running],
    ["blocked", counts.blocked],
    ["due runs", dueSchedules],
    ["stale hb", staleHeartbeats],
    ["task audit", taskAudits],
  ]
    .map(
      ([label, value]) =>
        `<div class="status-card"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`,
    )
    .join("");
}

function leaseTimestampLabel(lease) {
  return lease.status === "pending" ? "Requested" : "Issued";
}

function leaseDetailRows(lease) {
  return [
    `Agent: ${escapeHtml(agentName(lease.agent_id))}`,
    `Credential: ${escapeHtml(credentialName(lease.credential_id))}`,
    `${escapeHtml(leaseTimestampLabel(lease))}: ${escapeHtml(lease.issued_at)}`,
    `Expires: ${escapeHtml(lease.expires_at)}`,
    `TTL: ${escapeHtml(lease.ttl_seconds)}s`,
    `Intent: ${escapeHtml(lease.intent)}`,
  ].join("<br>");
}

function renderNavigation() {
  const page = currentPage();
  for (const pageId of Object.keys(ROUTES)) {
    const pageNode = $(`#${pageId}-page`);
    if (pageNode) {
      pageNode.hidden = pageId !== page;
    }
  }
  $("#surface-line").textContent = PAGE_META[page] || PAGE_META.overview;
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
  const passwordConfirmInput = authForm.elements.password_confirm;
  const isSetup = !state.setupComplete;

  if (state.authMode !== authMode) {
    authForm.reset();
    state.authMode = authMode;
  }

  $("#auth-view").hidden = Boolean(state.me);
  $("#app-view").hidden = !state.me;
  $("#workspace-nav").hidden = !state.me;
  $("#logout-button").hidden = !state.me;
  $("#refresh-button").hidden = !state.me;
  usernameInput.placeholder = isSetup ? "local-admin" : "username";
  passwordInput.placeholder = isSetup ? "create admin password" : "password";
  passwordInput.autocomplete = isSetup ? "new-password" : "current-password";
  passwordConfirmInput.required = isSetup;
  passwordConfirmInput.disabled = !isSetup;
  $("#password-confirm-field").hidden = !isSetup;
  $("#auth-title").textContent = isSetup ? "Set up admin" : "Log in";
  $("#auth-mode").textContent = isSetup ? "First local admin" : "Local admin console";
  $("#auth-detail").textContent = isSetup
    ? "Create the first local operator account for this Hivemind node."
    : "Sign in with the local username and password configured during setup.";
  $("#auth-submit").textContent = isSetup ? "create admin" : "sign in";
  $("#session-line").textContent = state.me ? `${state.me.username} / ${state.me.role}` : "Not signed in";
  renderNavigation();
}

function renderSelectors() {
  for (const selector of [
    '#lease-form select[name="agent_id"]',
    '#credential-form select[name="allowed_agents"]',
    '#oauth-credential-form select[name="allowed_agents"]',
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
  setText("#agent-count", state.agents.length);
  setText("#agents-page-count", state.agents.length);
  setText("#agents-idle-count", statusCount(state.agents, "idle"));
  setText("#agents-running-count", statusCount(state.agents, "running"));
  setText("#agents-blocked-count", statusCount(state.agents, "blocked"));
  setText("#nav-agents-count", state.agents.length);
  $("#agents-list").innerHTML = state.agents
    .map((agent) =>
      item(
        agent.name,
        `${escapeHtml(agent.role)}<br>ID: ${escapeHtml(agent.id)}<br>Prompt: ${escapeHtml(agent.system_prompt || "none")}`,
        [agent.status, agent.provider, agent.model],
      ),
    )
    .join("") || '<p class="meta">No agents yet.</p>';
}

function renderCredentials() {
  setText("#credential-count", state.credentials.length);
  setText("#nav-credentials-count", state.credentials.length);
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
      if (credential.policy.approval_required_actions.length) {
        pills.push(`approval gate: ${credential.policy.approval_required_actions.join(", ")}`);
      }
      return item(credential.name, credentialDetailRows(credential), pills);
    })
    .join("") || '<p class="meta">No credentials yet.</p>';
  setText("#credential-page-count", state.credentials.length);
}

function renderOAuthProviders() {
  const provider = activeOAuthProvider();
  const providerLabel = oauthProviderLabel(provider);
  const stateNode = $("#oauth-provider-state");
  const detailNode = $("#oauth-provider-detail");
  const button = $("#oauth-provider-button");
  $("#oauth-provider-title").textContent = provider?.available ? providerLabel : "OAuth provider";
  if (!provider) {
    stateNode.textContent = "missing";
    stateNode.dataset.state = "error";
    detailNode.textContent = "No OAuth provider profile is configured for this node.";
    button.disabled = true;
    button.textContent = "oauth unavailable";
    return;
  }
  stateNode.textContent = provider.available ? "ready" : "blocked";
  stateNode.dataset.state = provider.available ? "ready" : "error";
  button.disabled = !provider.available;
  button.textContent = provider.available ? "connect via oauth" : "oauth unavailable";
  detailNode.textContent = provider.available
    ? `${providerLabel} / scopes: ${provider.scopes.join(" ")}`
    : provider.reason || "OAuth provider is unavailable.";
}

function renderLeases() {
  setText("#lease-count", state.leases.length);
  $("#leases-list").innerHTML = state.leases
    .map((lease) =>
      item(
        lease.id,
        leaseDetailRows(lease),
        [lease.status, lease.action, `TTL ${lease.ttl_seconds}s`, lease.token_preview],
      ),
    )
    .join("") || '<p class="meta">No leases yet.</p>';
  setText("#credential-active-lease-count", state.leases.filter((lease) => lease.status === "active").length);
  setText("#credential-pending-lease-count", state.leases.filter((lease) => lease.status === "pending").length);
  setText("#credential-expired-lease-count", state.leases.filter((lease) => lease.status === "expired").length);
}

function renderPendingApprovals() {
  const pendingLeases = state.leases.filter((lease) => lease.status === "pending");
  $("#pending-approvals-list").innerHTML = pendingLeases
    .map((lease) => {
      const actions = `
        <div class="button-row">
          <button data-approve-lease="${escapeHtml(lease.id)}" type="button">Approve</button>
          <button data-deny-lease="${escapeHtml(lease.id)}" type="button">Deny</button>
        </div>`;
      return item(lease.id, leaseDetailRows(lease), [lease.status, lease.action, `TTL ${lease.ttl_seconds}s`], actions);
    })
    .join("") || '<p class="meta">No pending approvals.</p>';
}

function renderTasks() {
  const heartbeatsByTask = latestHeartbeatsByTask();
  setText("#task-count", state.tasks.length);
  setText("#tasks-page-count", state.tasks.length);
  setText("#tasks-running-count", statusCount(state.tasks, "running"));
  setText("#tasks-blocked-count", statusCount(state.tasks, "blocked"));
  setText("#tasks-stale-count", state.tasks.filter((task) => taskHeartbeatState(task) === "stale").length);
  setText("#nav-tasks-count", state.tasks.length);
  renderTaskHealth();
  $("#tasks-list").innerHTML = state.tasks
    .map((task) => {
      const heartbeatState = taskHeartbeatState(task);
      const lastHeartbeat = heartbeatsByTask.get(task.id);
      const editAction = `<button data-task-edit-toggle="${escapeHtml(task.id)}" type="button">${state.editingTaskIds.has(task.id) ? "close edit" : "edit task"}</button>`;
      const heartbeatAction = CLOSED_TASK_STATUSES.has(task.status)
        ? '<span class="meta">Heartbeat closed</span>'
        : `<button data-task-heartbeat="${escapeHtml(task.id)}" type="button">Heartbeat</button>`;
      const actions = `
        <div class="task-actions">
          <div class="status-row">
            ${renderTaskStatusButtons(task)}
          </div>
          <div class="button-row">
            ${editAction}
            ${heartbeatAction}
          </div>
          ${renderTaskEditForm(task)}
        </div>`;
      return item(
        task.title,
        `${escapeHtml(task.description || "No task description.")}<br>ID: ${escapeHtml(task.id)}<br>Agent: ${escapeHtml(taskAssignmentLabel(task))}<br>Credential: ${escapeHtml(taskCredentialLabel(task))}<br>Action: ${escapeHtml(task.action || "none")}<br>Intent: ${escapeHtml(task.intent || "none")}<br>Heartbeat SLA: ${taskHeartbeatLabel(task)}<br>Last heartbeat: ${escapeHtml(lastHeartbeat?.created_at || "none")}<br>Next heartbeat: ${escapeHtml(task.next_heartbeat_at || "none")}<br>Updated: ${escapeHtml(task.updated_at)}`,
        [task.status, task.priority, task.assigned_agent_id ? "assigned" : "unassigned", `heartbeat:${heartbeatState}`],
        actions,
      );
    })
    .join("") || '<p class="meta">No tasks yet.</p>';
}

function renderHeartbeats() {
  $("#heartbeats-list").innerHTML = state.heartbeats
    .map((heartbeat) =>
      `<article class="event"><strong>${escapeHtml(heartbeat.task_id)}</strong><div class="meta">Agent: ${escapeHtml(heartbeat.agent_id || "user")}<br>Note: ${escapeHtml(heartbeat.note)}<br>${escapeHtml(heartbeat.created_at)}</div></article>`,
    )
    .join("") || '<p class="meta">No heartbeats yet.</p>';
}

function renderSchedules() {
  const dueSchedules = state.schedules.filter(isScheduleDue);
  setText("#schedule-count", state.schedules.length);
  setText("#schedules-page-count", state.schedules.length);
  setText("#schedules-enabled-count", state.schedules.filter((schedule) => schedule.enabled).length);
  setText("#schedules-due-count", dueSchedules.length);
  setText("#nav-schedules-count", state.schedules.length);
  $("#schedules-list").innerHTML = state.schedules
    .map((schedule) => {
      const due = isScheduleDue(schedule);
      const assignedAgent = schedule.assigned_agent_id ? agentName(schedule.assigned_agent_id) : "unassigned";
      const credential = schedule.credential_id ? credentialName(schedule.credential_id) : "none";
      const actions = `
        <div class="button-row">
          <button data-schedule-enabled="${escapeHtml(schedule.id)}" data-enabled="${schedule.enabled ? "false" : "true"}" type="button">
            ${schedule.enabled ? "Pause" : "Enable"}
          </button>
        </div>`;
      return item(
        schedule.name,
        `ID: ${escapeHtml(schedule.id)}<br>Task: ${escapeHtml(schedule.task_title)}<br>Agent: ${escapeHtml(assignedAgent)}<br>Credential: ${escapeHtml(credential)}<br>Action: ${escapeHtml(schedule.action || "none")}<br>Intent: ${escapeHtml(schedule.intent || "none")}<br>Interval: ${escapeHtml(schedule.interval_seconds)}s<br>Catch-up: ${escapeHtml(scheduleCatchUpPolicyLabel(schedule.catch_up_policy))}<br>Last run: ${escapeHtml(schedule.last_run_at || "none")}<br>Next run: ${escapeHtml(schedule.next_run_at)}`,
        [
          schedule.enabled ? "enabled" : "paused",
          schedule.priority,
          scheduleCatchUpPolicyLabel(schedule.catch_up_policy),
          due ? "due now" : "scheduled",
        ],
        actions,
      );
    })
    .join("") || '<p class="meta">No schedules yet.</p>';
}

function renderAudit() {
  setText("#audit-count", state.auditEvents.length);
  setText("#audit-page-count", state.auditEvents.length);
  setText("#audit-denied-count", state.auditEvents.filter((event) => event.decision === "denied").length);
  setText("#task-audit-count", state.auditEvents.filter((event) => event.type.startsWith("task.")).length);
  setText("#schedule-audit-count", state.auditEvents.filter((event) => event.type.startsWith("schedule.")).length);
  setText("#nav-audit-count", state.auditEvents.length);
  $("#audit-list").innerHTML = state.auditEvents
    .map(
      (event) =>
        `<article class="event"><strong>${escapeHtml(event.type)}</strong><div class="meta">${escapeHtml(event.decision)}: ${escapeHtml(event.reason)}<br>Actor: ${escapeHtml(event.actor_id)} -> Target: ${escapeHtml(event.target_id)}${Object.entries(event.metadata || {}).length ? `<br>${Object.entries(event.metadata)
          .map(([key, value]) => `${escapeHtml(key)}: ${escapeHtml(typeof value === "string" ? value : JSON.stringify(value))}`)
          .join("<br>")}` : ""}<br>${escapeHtml(event.created_at)}</div></article>`,
    )
    .join("") || '<p class="meta">No audit events yet.</p>';
}

function renderCredentialAudit() {
  const events = state.auditEvents.filter((event) => event.type.startsWith("credential."));
  $("#credential-denied-count").textContent = events.filter((event) => event.decision === "denied").length;
  $("#credential-audit-list").innerHTML = events
    .map((event) => {
      const action = event.metadata?.action ? `<br>Action: ${escapeHtml(event.metadata.action)}` : "";
      const ttl = Number.isFinite(event.metadata?.ttl_seconds) ? `<br>TTL: ${escapeHtml(event.metadata.ttl_seconds)}s` : "";
      const leaseId = event.metadata?.lease_id ? `<br>Lease: ${escapeHtml(event.metadata.lease_id)}` : "";
      return `<article class="event"><strong>${escapeHtml(event.type)}</strong><div class="meta">${escapeHtml(event.decision)}: ${escapeHtml(event.reason)}<br>Actor: ${escapeHtml(event.actor_id)} -> Target: ${escapeHtml(event.target_id)}${action}${ttl}${leaseId}<br>${escapeHtml(event.created_at)}</div></article>`;
    })
    .join("") || '<p class="meta">No credential audit events yet.</p>';
}

function renderOverview() {
  const activeTasks = state.tasks.filter((task) => !["done", "failed", "cancelled"].includes(task.status)).slice(0, 5);
  const dueSchedules = state.schedules.filter(isScheduleDue).slice(0, 5);
  const recentAudit = state.auditEvents.slice(0, 5);

  $("#overview-agents-list").innerHTML = state.agents
    .slice(0, 5)
    .map((agent) => item(agent.name, `ID: ${escapeHtml(agent.id)}<br>${escapeHtml(agent.role)}`, [agent.status, agent.provider, agent.model]))
    .join("") || '<p class="meta">No agents yet.</p>';

  $("#overview-tasks-list").innerHTML = activeTasks
    .map((task) => {
      const pills = [task.status, task.priority];
      if (isTaskStale(task)) {
        pills.push("stale heartbeat");
      }
      return item(
        task.title,
        `Agent: ${escapeHtml(task.assigned_agent_id ? agentName(task.assigned_agent_id) : "unassigned")}<br>Next heartbeat: ${escapeHtml(task.next_heartbeat_at || "none")}`,
        pills,
      );
    })
    .join("") || '<p class="meta">No active tasks.</p>';

  $("#overview-schedules-list").innerHTML = dueSchedules
    .map((schedule) =>
      item(
        schedule.name,
        `Next run: ${escapeHtml(schedule.next_run_at)}<br>Task: ${escapeHtml(schedule.task_title)}`,
        [schedule.enabled ? "enabled" : "paused", scheduleCatchUpPolicyLabel(schedule.catch_up_policy)],
      ),
    )
    .join("") || '<p class="meta">No schedules due now.</p>';

  $("#overview-audit-list").innerHTML = recentAudit
    .map(
      (event) =>
        `<article class="event"><strong>${escapeHtml(event.type)}</strong><div class="meta">${escapeHtml(event.decision)}: ${escapeHtml(event.reason)}<br>${escapeHtml(event.created_at)}</div></article>`,
    )
    .join("") || '<p class="meta">No audit events yet.</p>';
}

function renderConfig() {
  const reviewer = state.config?.intent_reviewer;
  if (!reviewer) {
    $("#reviewer-config").textContent = "local / deterministic-policy / no credential ref";
    return;
  }
  const provider = reviewer.provider || "local";
  const model = reviewer.model || "deterministic-policy";
  const credentialRef = reviewer.credential_ref_preview || "no credential ref";
  $("#reviewer-config").textContent = `${provider} / ${model} / ${credentialRef}`;
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
  renderPendingApprovals();
  renderTasks();
  renderHeartbeats();
  renderSchedules();
  renderAudit();
  renderCredentialAudit();
  renderOverview();
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
  const isSetup = !state.setupComplete;
  const path = isSetup ? "/auth/setup" : "/auth/login";
  if (isSetup && payload.password !== payload.password_confirm) {
    toast("password confirmation does not match");
    return;
  }
  if (!isSetup) {
    delete payload.password_confirm;
  }
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    await refresh();
    toast(isSetup ? "Admin created." : "Signed in.");
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
  toast("Agent registered.");
});

$("#credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const template = credentialTemplates[state.selectedCredentialTemplate];
  const payload = readForm(form);
  const templatePayload = template.buildPayload(form);
  payload.name = payload.name.trim();
  payload.provider = templatePayload.provider;
  delete payload.secret_ref;
  delete payload.secret_value;
  payload.secret_ref = templatePayload.secret_ref ?? null;
  payload.secret_value = templatePayload.secret_value ?? null;
  payload.allowed_agents = selectedValues(form.elements.allowed_agents);
  payload.allowed_actions = splitCsv(payload.allowed_actions);
  payload.approval_required_actions = splitCsv(payload.approval_required_actions);
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

$("#oauth-credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const provider = activeOAuthProvider();
  if (!provider || !provider.available) {
    toast("OAuth provider is not available.");
    return;
  }
  const payload = readForm(form);
  payload.provider = provider.id;
  payload.allowed_agents = selectedValues(form.elements.allowed_agents);
  payload.allowed_actions = splitCsv(payload.allowed_actions);
  payload.approval_required_actions = splitCsv(payload.approval_required_actions);
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
    $('#action-form input[name="lease_token"]').value = lease.lease_token || "";
    $('#action-form input[name="action"]').value = lease.action;
    await refresh();
    toast(lease.lease_token ? "Lease issued." : "Lease request queued for approval.");
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
    toast("Lease matched action.");
  } catch (error) {
    $("#action-result").textContent = error.message;
    await refresh();
    toast(error.message);
  }
});

$("#pending-approvals-list").addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  if (target.dataset.approveLease) {
    try {
      const lease = await api(`/credential-leases/${target.dataset.approveLease}/approve`, { method: "POST" });
      $('#action-form input[name="lease_token"]').value = lease.lease_token;
      $('#action-form input[name="action"]').value = lease.action;
      await refresh();
      toast("Lease approved.");
    } catch (error) {
      await refresh();
      toast(error.message);
    }
  }
  if (target.dataset.denyLease) {
    try {
      await api(`/credential-leases/${target.dataset.denyLease}/deny`, { method: "POST" });
      await refresh();
      toast("Lease denied.");
    } catch (error) {
      await refresh();
      toast(error.message);
    }
  }
});

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = normalizeTaskPayload(readForm(event.currentTarget));
  await api("/tasks", { method: "POST", body: JSON.stringify(payload) });
  await refresh();
  toast("Task created.");
});

$("#tasks-list").addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  try {
    if (target.dataset.taskEditToggle) {
      toggleTaskEdit(target.dataset.taskEditToggle);
      return;
    }
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
  } catch (error) {
    await refresh();
    toast(error.message);
  }
});

$("#tasks-list").addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const taskId = form.dataset.taskEditForm;
  if (!taskId) return;
  event.preventDefault();
  try {
    const payload = normalizeTaskPayload(readForm(form));
    await api(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify(payload) });
    state.editingTaskIds.delete(taskId);
    await refresh();
    toast("Task details updated.");
  } catch (error) {
    state.editingTaskIds.add(taskId);
    await refresh();
    toast(error.message);
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
