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
      if (credential.policy.approval_required_actions.length) {
        pills.push(`approval gate: ${credential.policy.approval_required_actions.join(", ")}`);
      }
      return item(credential.name, credentialDetailRows(credential), pills);
    })
    .join("") || '<p class="meta">No credentials yet.</p>';
  $("#credential-page-count").textContent = state.credentials.length;
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
  $("#lease-count").textContent = state.leases.length;
  $("#leases-list").innerHTML = state.leases
    .map((lease) =>
      item(
        lease.id,
        leaseDetailRows(lease),
        [lease.status, lease.action, `TTL ${lease.ttl_seconds}s`, lease.token_preview],
      ),
    )
    .join("") || '<p class="meta">No leases yet.</p>';
  $("#credential-active-lease-count").textContent = state.leases.filter((lease) => lease.status === "active").length;
  $("#credential-pending-lease-count").textContent = state.leases.filter((lease) => lease.status === "pending").length;
  $("#credential-expired-lease-count").textContent = state.leases.filter((lease) => lease.status === "expired").length;
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
        `Every ${escapeHtml(schedule.interval_seconds)}s<br>Catch-up: ${escapeHtml(scheduleCatchUpPolicyLabel(schedule.catch_up_policy))}<br>Next run: ${escapeHtml(schedule.next_run_at)}<br>Last run: ${escapeHtml(schedule.last_run_at || "never")}<br>Task: ${escapeHtml(schedule.task_title)}`,
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
