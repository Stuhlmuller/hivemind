const state = {
  agents: [],
  credentials: [],
  leases: [],
  auditEvents: [],
  config: null,
  lastLeaseToken: "",
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
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

function item(title, meta, pills = []) {
  const pillMarkup = pills.length
    ? `<div class="pill-row">${pills.map((pill) => `<span class="pill">${escapeHtml(pill)}</span>`).join("")}</div>`
    : "";
  return `<article class="item"><strong>${escapeHtml(title)}</strong><div class="meta">${meta}</div>${pillMarkup}</article>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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
    .join("");

  const select = $('#lease-form select[name="agent_id"]');
  select.innerHTML = state.agents
    .map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)} (${escapeHtml(agent.id)})</option>`)
    .join("");
}

function renderCredentials() {
  $("#credential-count").textContent = state.credentials.length;
  $("#credentials-list").innerHTML = state.credentials
    .map((credential) =>
      item(
        credential.name,
        `ID: ${escapeHtml(credential.id)}<br>Provider: ${escapeHtml(credential.provider)}`,
        credential.policy.allowed_actions,
      ),
    )
    .join("");

  const select = $('#lease-form select[name="credential_id"]');
  select.innerHTML = state.credentials
    .map((credential) => `<option value="${escapeHtml(credential.id)}">${escapeHtml(credential.name)}</option>`)
    .join("");
}

function renderLeases() {
  $("#lease-count").textContent = state.leases.length;
  $("#leases-list").innerHTML = state.leases.length
    ? state.leases
        .map((lease) =>
          item(
            lease.id,
            `Agent: ${escapeHtml(lease.agent_id)}<br>Action: ${escapeHtml(lease.action)}<br>Expires: ${escapeHtml(lease.expires_at)}`,
            [lease.status, lease.token_preview],
          ),
        )
        .join("")
    : '<p class="meta">No leases yet.</p>';
}

function renderAudit() {
  $("#audit-count").textContent = state.auditEvents.length;
  $("#audit-list").innerHTML = state.auditEvents.length
    ? state.auditEvents
        .slice()
        .reverse()
        .map(
          (event) =>
            `<article class="event"><strong>${escapeHtml(event.type)}</strong><div class="meta">${escapeHtml(event.decision)}: ${escapeHtml(event.reason)}<br>Actor: ${escapeHtml(event.actor_id)} -> Target: ${escapeHtml(event.target_id)}</div></article>`,
        )
        .join("")
    : '<p class="meta">No broker activity yet.</p>';
}

function renderConfig() {
  const reviewer = state.config?.intent_reviewer;
  $("#reviewer-config").textContent = reviewer
    ? `${reviewer.provider} / ${reviewer.model}`
    : "No reviewer configured";
}

function render() {
  renderConfig();
  renderAgents();
  renderCredentials();
  renderLeases();
  renderAudit();
}

async function refresh() {
  const [config, agents, credentials, leases, auditEvents] = await Promise.all([
    api("/config"),
    api("/agents"),
    api("/credentials"),
    api("/credential-leases"),
    api("/audit-events"),
  ]);
  Object.assign(state, { config, agents, credentials, leases, auditEvents });
  render();
}

function readForm(form) {
  return Object.fromEntries(new FormData(form).entries());
}

$("#refresh-button").addEventListener("click", async () => {
  await refresh();
  toast("Runtime refreshed.");
});

$("#spawn-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm(event.currentTarget);
  await api("/agents", { method: "POST", body: JSON.stringify(payload) });
  await refresh();
  toast("Agent joined the swarm.");
});

$("#lease-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm(event.currentTarget);
  payload.ttl_seconds = Number(payload.ttl_seconds);
  try {
    const lease = await api("/credential-leases", { method: "POST", body: JSON.stringify(payload) });
    state.lastLeaseToken = lease.lease_token;
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

refresh().catch((error) => toast(error.message));
