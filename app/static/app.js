const chat = document.getElementById("chat");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendButton = document.getElementById("send");

let busy = false;
let phase = "chat";

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  const options = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function inline(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_(.+?)_(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
}

// Minimal markdown: paragraphs, **bold**, _italic_ and "- " lists.
function md(text) {
  let html = "";
  let inList = false;
  for (const line of escapeHtml(text || "").split("\n")) {
    const item = line.match(/^\s*[-•*]\s+(.*)/);
    if (item) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(item[1])}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return inList ? html + "</ul>" : html;
}

function el(tag, className, html) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function scrollDown() {
  chat.scrollTop = chat.scrollHeight;
}

// ---------------------------------------------------------------- rendering

function renderStores(data) {
  const cards = el("div", "cards");
  data.options.forEach((o, i) => {
    const card = el("div", i === 0 ? "card best" : "card");
    const meta = [o.rating && `⭐ ${o.rating}`, o.eta, `Entrega ${o.delivery_fee}`].filter(Boolean).join(" · ");
    card.innerHTML = `
      ${i === 0 ? '<span class="badge">Melhor opção</span>' : ""}
      <h3>${o.number}. ${escapeHtml(o.name)}</h3>
      <div class="meta">${escapeHtml(meta)}</div>
      <div class="price">${o.total} <small>c/ entrega</small></div>
      <div class="coverage">${o.found}/${o.wanted} itens · produtos ${o.subtotal}</div>
      ${o.missing.length ? `<div class="chips">${o.missing.map((m) => `<span class="chip">falta ${escapeHtml(m)}</span>`).join("")}</div>` : ""}
      ${o.minimum ? `<div class="warn">Pedido mínimo do mercado: ${o.minimum}</div>` : ""}
      <details><summary>Ver itens</summary><ul>${o.items.map((it) => `<li>${escapeHtml(it)}</li>`).join("")}</ul></details>
    `;
    const button = el("button", "btn", "Escolher este mercado");
    button.type = "button";
    button.dataset.storeBtn = o.number;
    button.addEventListener("click", () => chooseStore(o.number, o.name));
    card.appendChild(button);
    cards.appendChild(card);
  });
  return cards;
}

function renderSummary(data) {
  const box = el("div", "summary");
  box.innerHTML = `
    <dl>
      <dt>Mercado</dt><dd>${escapeHtml(data.store)}</dd>
      <dt>Entrega</dt><dd>${escapeHtml(data.address)} · o quanto antes</dd>
      <dt>Pagamento</dt><dd>${escapeHtml(data.payment)}</dd>
    </dl>
    <details open><summary>Itens (${data.items.length})</summary><ul>${data.items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul></details>
    ${data.warnings.map((w) => `<div class="warn">${escapeHtml(w)}</div>`).join("")}
    <div class="totals">
      ${data.lines.map((l) => `<span class="${l.label.startsWith("Total") ? "grand" : ""}">${escapeHtml(l.label)}</span><span class="${l.label.startsWith("Total") ? "grand" : ""}">${escapeHtml(l.value)}</span>`).join("")}
    </div>
    <details><summary>Detalhes técnicos do carrinho</summary><pre>${escapeHtml(data.raw)}</pre></details>
  `;
  const button = el("button", "btn", "Confirmar pedido");
  button.type = "button";
  button.dataset.confirmBtn = "1";
  button.addEventListener("click", confirmOrder);
  box.appendChild(button);
  return box;
}

function render(message) {
  const row = el("div", `msg ${message.role}`);
  row.dataset.kind = message.kind;
  const bubble = el("div", "bubble", md(message.text));
  if (message.kind === "stores" && message.data) {
    bubble.classList.add("wide");
    bubble.appendChild(renderStores(message.data));
  } else if (message.kind === "summary" && message.data) {
    bubble.classList.add("wide");
    bubble.appendChild(renderSummary(message.data));
  } else if (message.kind === "login") {
    const link = el("a", "btn", "Conectar iFood");
    link.href = "/login";
    link.style.display = "inline-block";
    link.style.marginTop = "8px";
    link.style.textDecoration = "none";
    bubble.appendChild(link);
  }
  row.appendChild(bubble);
  chat.appendChild(row);
}

function lastOf(kind) {
  const all = chat.querySelectorAll(`.msg[data-kind="${kind}"]`);
  return all[all.length - 1];
}

// Only the latest comparison / summary stays clickable, and only in the right phase.
function refreshControls() {
  const stores = lastOf("stores");
  const summary = lastOf("summary");
  chat.querySelectorAll("[data-store-btn]").forEach((b) => {
    b.disabled = busy || !["compare", "checkout"].includes(phase) || b.closest(".msg") !== stores;
  });
  chat.querySelectorAll("[data-confirm-btn]").forEach((b) => {
    b.disabled = busy || phase !== "checkout" || b.closest(".msg") !== summary;
    if (phase === "ordered" && b.closest(".msg") === summary) b.textContent = "Pedido confirmado ✓";
  });
  input.disabled = busy;
  sendButton.disabled = busy;
}

function applyState(state) {
  phase = state.phase;
  document.getElementById("connect").hidden = state.connected;
  document.getElementById("connected").hidden = !state.connected;
  state.messages.forEach(render);
  refreshControls();
  scrollDown();
}

function showTyping(label) {
  const node = document.getElementById("typing-template").content.firstElementChild.cloneNode(true);
  node.querySelector(".typing-label").textContent = label || "";
  chat.appendChild(node);
  scrollDown();
  return node;
}

async function run(userText, label, request) {
  if (busy) return;
  busy = true;
  if (userText) render({ role: "user", kind: "text", text: userText });
  refreshControls();
  const typing = showTyping(label);
  try {
    applyState(await request());
  } catch (error) {
    render({ role: "bot", kind: "text", text: "Ops, algo deu errado na conexão com o servidor. Tenta de novo?" });
    console.error(error);
  } finally {
    typing.remove();
    busy = false;
    refreshControls();
    scrollDown();
    input.focus();
  }
}

// ---------------------------------------------------------------- actions

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  const shown = phase === "need_document" ? text.replace(/\d/g, "•") : text;
  run(shown, "", () => api("/api/chat", { message: text }));
});

function chooseStore(number, name) {
  run(`Quero o mercado ${name}`, "Montando seu carrinho…", () => api("/api/choose_store", { number }));
}

function confirmOrder() {
  run("✅ Confirmar pedido", "Finalizando o pedido…", () => api("/api/confirm", {}));
}

document.getElementById("reset").addEventListener("click", async () => {
  if (busy) return;
  chat.innerHTML = "";
  applyState(await api("/api/reset", {}));
});

// Long operations (searching several stores) get a hint after a few seconds.
const observer = new MutationObserver(() => {
  const label = chat.querySelector(".typing .typing-label");
  if (label && !label.textContent && !label.dataset.timer) {
    label.dataset.timer = setTimeout(() => { label.textContent = "só um instante…"; }, 4000);
  }
});
observer.observe(chat, { childList: true });

api("/api/state").then(applyState);
