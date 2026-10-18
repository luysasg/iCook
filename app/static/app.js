const chat = document.getElementById("chat");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendButton = document.getElementById("send");
const authBanner = document.getElementById("auth-banner");

let busy = false;
let state = { phase: "chat", connected: false, dry_run: false, needs_resume: false, messages: [] };
const revisions = new Map(); // message id -> rev rendered
let trackTimer = null;

const ALLERGEN = {
  compatible: ["Compatível*", "tag-ok"],
  uncertain: ["Verificar rótulo", "tag-unsure"],
  avoid: ["Evitar", "tag-avoid"],
};
const ALLERGEN_NOTE = "*Rótulos baseados nos dados do cardápio, não garantem ausência de alérgenos. Confira sempre a embalagem.";

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  const options = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

// Builds DOM nodes; strings become text nodes (never HTML) unless passed as `html`.
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// ---------------------------------------------------------------- markdown
// Escape everything first, then add a fixed set of tags: the output can't contain HTML from the text.
// Unclosed markers (e.g. a lone "**") are left as plain text instead of breaking the layout.

function inlineMd(text) {
  return text
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*(?=\S)([^*\n]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__(?=\S)([^_\n]+?)__/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])\*(?=\S)([^*\n]+?)\*(?![\w*])/g, "$1<em>$2</em>")
    .replace(/(^|[^\w])_(?=\S)([^_\n]+?)_(?!\w)/g, "$1<em>$2</em>");
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((c) => inlineMd(c.trim()));
}

function md(text) {
  const lines = escapeHtml(text || "").split("\n");
  let html = "";
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (paragraph.length) html += `<p>${inlineMd(paragraph.join("<br>"))}</p>`;
    paragraph = [];
  };
  const closeList = () => {
    if (list) html += `</${list}>`;
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (!line.trim()) { flush(); closeList(); continue; }
    if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) {
      flush(); closeList();
      const tag = m[1].length <= 2 ? "h3" : "h4";
      html += `<${tag}>${inlineMd(m[2])}</${tag}>`;
      continue;
    }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); closeList(); html += "<hr>"; continue; }
    if (line.trim().startsWith("|") && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || "")) {
      flush(); closeList();
      const head = tableCells(line).map((c) => `<th>${c}</th>`).join("");
      let body = "";
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        body += `<tr>${tableCells(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`;
        i++;
      }
      i--;
      html += `<div class="md-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
      continue;
    }
    const bullet = line.match(/^\s*[-*•+]\s+(.*)/);
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)/);
    if (bullet || numbered) {
      flush();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        html += kind === "ol" && numbered[1] !== "1" ? `<ol start="${Number(numbered[1])}">` : `<${kind}>`;
        list = kind;
      }
      html += `<li>${inlineMd((bullet || numbered)[bullet ? 1 : 2])}</li>`;
      continue;
    }
    closeList();
    paragraph.push(line.trim());
  }
  flush();
  closeList();
  return html;
}

// ---------------------------------------------------------------- small pieces

function bubble(text, extraClass) {
  return h("div", { class: `bubble${extraClass ? ` ${extraClass}` : ""}`, html: md(text) });
}

function allergenTag(value) {
  const entry = ALLERGEN[value];
  return entry ? h("span", { class: `tag ${entry[1]}`, title: ALLERGEN_NOTE }, entry[0]) : null;
}

// Marks a control as usable only in some phases, and only on the latest card of its kind.
function control(node, phases) {
  node.dataset.phases = phases;
  return node;
}

function chipsFor(message) {
  const chips = (message.data && message.data.chips) || [];
  if (!chips.length) return null;
  return h("div", { class: "chips", role: "group", "aria-label": "Respostas sugeridas" },
    chips.map((text) => h("button", { type: "button", class: "chip", onclick: () => sendText(text) }, text)));
}

function radioCard(name, options, buttonLabel, phases, onSubmit, selected) {
  const checkedIndex = Math.max(0, options.findIndex((o) => o.value === selected));
  const fieldset = h("fieldset", { class: "options" },
    h("legend", { class: "sr-only" }, buttonLabel),
    options.map((o, i) => h("label", { class: "option" },
      control(h("input", { type: "radio", name, value: o.value, checked: i === checkedIndex }), phases),
      h("span", null, o.label, o.hint ? h("span", { class: "hint" }, o.hint) : null))));
  const button = control(h("button", {
    type: "button", class: "btn btn-primary btn-block",
    onclick: () => {
      const picked = fieldset.querySelector("input:checked");
      const option = picked && options.find((o) => String(o.value) === picked.value);
      if (option) onSubmit(option.value, option.label);
    },
  }, buttonLabel), phases);
  return h("div", { class: "card" }, fieldset, button);
}

// ---------------------------------------------------------------- cards

function recipeCard(data) {
  const checklist = h("ul", { class: "checklist" }, data.ingredients.map((ing) => h("li", null,
    h("label", { class: "check" },
      control(h("input", { type: "checkbox", value: ing.number, checked: data.have.includes(ing.number),
        "aria-label": `Já tenho ${ing.name}`, onchange: () => updateCount() }), "pantry"),
      h("span", { class: "num" }, `${ing.number}.`),
      h("span", { class: "name" }, ing.name),
      h("span", { class: "amount" }, ing.amount)))));
  const button = control(h("button", { type: "button", class: "btn btn-primary btn-block", onclick: () => {
    const have = [...checklist.querySelectorAll("input:checked")].map((c) => c.value);
    sendText(have.length ? `Já tenho: ${have.join(", ")}` : "Não tenho nenhum");
  } }), "pantry");
  function updateCount() {
    const missing = data.ingredients.length - checklist.querySelectorAll("input:checked").length;
    button.textContent = missing ? `Comprar o que falta (${missing} ${missing === 1 ? "item" : "itens"})` : "Já tenho tudo";
  }
  updateCount();
  return h("div", { class: "card recipe" },
    h("h3", null, data.title),
    data.description ? h("p", { class: "card-sub" }, data.description) : null,
    data.servings ? h("div", { class: "meta" }, `Rende ${data.servings} ${data.servings === 1 ? "porção" : "porções"}`) : null,
    h("p", { class: "section-title" }, "Ingredientes · marque o que você já tem"),
    checklist,
    data.steps.length ? h("details", { open: true },
      h("summary", null, `Modo de preparo (${data.steps.length} passos)`),
      h("ol", { class: "steps" }, data.steps.map((step) => h("li", null, step)))) : null,
    button);
}

function storeCards(data) {
  const cards = data.options.map((o, i) => {
    const meta = h("div", { class: "meta" },
      o.rating ? h("span", null, h("span", { class: "star", "aria-hidden": "true" }, "★ "), o.rating) : null,
      o.eta ? h("span", null, o.eta) : null,
      h("span", null, `Entrega ${o.delivery_fee}`));
    const missing = o.missing.length
      ? h("div", { class: "tags" }, o.missing.map((m) => h("span", { class: "tag tag-missing" }, `sem ${m}`)))
      : null;
    const items = h("details", null, h("summary", null, `Ver itens (${o.items.length})`),
      h("ul", { class: "items" }, o.items.map((it) => h("li", null, h("span", null, it.label), allergenTag(it.allergen)))));
    const button = control(h("button", {
      type: "button", class: `btn ${i === 0 ? "btn-primary" : "btn-secondary"} btn-block`,
      "aria-label": `Escolher ${o.name}`, onclick: () => chooseStore(o.number, o.name),
    }, "Escolher este mercado"), "compare,payment,voucher,checkout");
    return h("article", { class: `card store${i === 0 ? " best" : ""}`, "aria-label": o.name },
      h("div", { class: "store-head" }, h("h3", null, o.name), i === 0 ? h("span", { class: "tag tag-best" }, "Melhor opção") : null),
      meta,
      h("div", { class: "price" }, o.total, " ", h("small", null, "com entrega")),
      h("div", { class: "meta" }, `${o.found}/${o.wanted} ingredientes · produtos ${o.subtotal}`),
      missing,
      o.minimum ? h("div", { class: "warn" }, `Pedido mínimo do mercado: ${o.minimum}`) : null,
      items,
      button);
  });
  return h("div", { class: "stack" }, cards, data.restrictions ? h("p", { class: "footnote" }, ALLERGEN_NOTE) : null);
}

function addressChoices(data) {
  return h("div", { class: "stack" }, data.options.map((a) => control(h("button", {
    type: "button", class: "btn btn-secondary btn-block", style: "justify-content:flex-start",
    onclick: () => chooseAddress(a.number, a.label),
  }, `📍 ${a.label}`), "address")));
}

function paymentCard(data) {
  const options = data.options.map((o) => ({
    value: o.number, label: o.label,
    hint: [o.number === data.selected ? "Salva na sua conta" : null, o.offline ? "Pague na entrega" : null]
      .filter(Boolean).join(" · ") || null,
  }));
  return radioCard("payment", options, "Usar esta forma de pagamento", "payment,voucher,checkout",
    (number, label) => run(`Pagar com ${label}`, () => api("/api/choose_payment", { number })), data.selected);
}

function voucherCard(data) {
  const options = data.options.map((o) => ({ value: o.number, label: o.label }))
    .concat([{ value: 0, label: "Seguir sem cupom" }]);
  return radioCard("voucher", options, "Continuar", "voucher",
    (number, label) => run(number ? `Usar o cupom ${label}` : "Seguir sem cupom",
      () => api("/api/choose_voucher", { number })));
}

function summaryCard(data) {
  const total = data.lines.find((l) => l.label.startsWith("Total"));
  const documentInput = control(h("input", { id: `doc-${Math.random().toString(36).slice(2, 8)}`, type: "text",
    autocomplete: "off", inputmode: "text", maxlength: "20", placeholder: "Opcional" }), "checkout");
  const button = control(h("button", { type: "button", class: "btn btn-primary btn-block", "data-confirm": "1",
    onclick: () => confirmOrder(documentInput.value.trim()) },
  `Confirmar pedido${total ? ` · ${total.value}` : ""}`), "checkout");
  return h("article", { class: "card summary", "aria-label": "Resumo do pedido" },
    h("h3", null, data.store),
    data.eta ? h("p", { class: "card-sub" }, `Entrega em ${data.eta}`) : null,
    h("dl", { class: "rows" },
      h("dt", null, "Entregar em"), h("dd", null, data.address),
      h("dt", null, "Pagamento"), h("dd", null, data.payment),
      data.voucher ? [h("dt", null, "Cupom"), h("dd", null, data.voucher)] : null),
    h("p", { class: "section-title" }, `Itens (${data.items.length})`),
    h("ul", { class: "items" }, data.items.map((it) => h("li", null,
      h("span", null, h("span", { class: "qty" }, `${it.quantity}×`), it.name, " ", allergenTag(it.allergen)),
      h("span", null, it.price)))),
    data.missing.length ? h("div", { class: "warn" }, `Vai sem: ${data.missing.join(", ")}`) : null,
    data.warnings.map((w) => h("div", { class: "warn" }, w)),
    h("div", { class: "totals" }, data.lines.map((l) => {
      const grand = l.label.startsWith("Total") ? "grand" : null;
      return [h("span", { class: grand }, l.label), h("span", { class: grand }, l.value)];
    })),
    h("label", { class: "field", for: documentInput.id }, "CPF/CNPJ na nota, ou passaporte (opcional)", documentInput),
    data.dry_run ? h("p", { class: "test-mode", role: "note" }, "Modo de teste: seu pedido não será feito.") : null,
    button);
}

function trackerCard(data) {
  const steps = h("ol", { class: "tracker-steps" }, data.steps.map((label, i) => h("li", {
    class: data.cancelled ? null : i < data.step ? "done" : i === data.step ? "current" : null,
    "aria-current": !data.cancelled && i === data.step ? "step" : null,
  }, label)));
  const updated = new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  return h("article", { class: `card tracker${data.cancelled ? " cancelled" : ""}`, "aria-label": "Status do pedido" },
    h("div", { class: "store-head" },
      h("h3", null, data.short_code ? `Pedido #${data.short_code}` : "Seu pedido"),
      data.dry_run ? h("span", { class: "tag tag-unsure" }, "Simulação") : null),
    steps,
    h("div", { class: "status-box", "aria-live": "polite" }, data.description),
    h("p", { class: "footnote" }, data.poll_after
      ? `Atualizado às ${updated} · atualiza sozinho a cada ${data.poll_after}s`
      : `Atualizado às ${updated} · acompanhamento encerrado`));
}

function renderMessage(message) {
  const row = h("div", { class: `msg ${message.role}`, "data-id": message.id, "data-kind": message.kind });
  const data = message.data || {};
  if (message.role === "user") {
    row.append(bubble(message.text));
    return row;
  }
  switch (message.kind) {
    case "recipe":
      row.append(recipeCard(data), bubble(message.text));
      break;
    case "stores":
      row.append(bubble(message.text), storeCards(data));
      break;
    case "addresses":
      row.append(bubble(message.text), addressChoices(data));
      break;
    case "payment":
      row.append(bubble(message.text), paymentCard(data));
      break;
    case "vouchers":
      row.append(bubble(message.text), voucherCard(data));
      break;
    case "summary":
      row.append(bubble(message.text), summaryCard(data));
      break;
    case "tracker":
      row.append(trackerCard(data));
      break;
    case "order":
      row.classList.add("order");
      row.append(bubble(message.text));
      break;
    case "login":
      row.append(bubble(message.text), h("div", { class: "card connect-card", "data-login": "1" },
        h("p", null, "O login abre numa janela do iFood. Esta conversa continua aqui."),
        h("button", { type: "button", class: "btn btn-primary btn-block", onclick: startLogin }, "Conectar ao iFood")));
      break;
    default:
      row.append(bubble(message.text));
  }
  const chips = chipsFor(message);
  if (chips) row.append(chips);
  return row;
}

function notice(html) {
  const row = h("div", { class: "msg bot local" }, h("div", { class: "bubble", html }));
  chat.append(row);
  scrollDown();
}

// ---------------------------------------------------------------- state

function scrollDown() {
  chat.scrollTop = chat.scrollHeight;
}

function lastOfKind(kind) {
  const all = chat.querySelectorAll(`.msg[data-kind="${kind}"]`);
  return all[all.length - 1];
}

function refreshControls() {
  const last = [...chat.querySelectorAll(".msg[data-id]")].pop();
  chat.querySelectorAll("[data-phases]").forEach((node) => {
    const message = node.closest(".msg");
    const usable = node.dataset.phases.split(",").includes(state.phase) && message === lastOfKind(message.dataset.kind);
    node.disabled = busy || !usable;
  });
  // Quick replies only make sense under the latest message.
  chat.querySelectorAll(".chips").forEach((chips) => {
    chips.hidden = chips.closest(".msg") !== last;
    chips.querySelectorAll("button").forEach((b) => { b.disabled = busy; });
  });
  // Only the latest connect card, and only while disconnected.
  chat.querySelectorAll("[data-login]").forEach((card) => {
    card.hidden = state.connected || card.closest(".msg") !== lastOfKind("login");
  });
  const summary = lastOfKind("summary");
  if (summary && ["ordered", "need_document"].includes(state.phase)) {
    const button = summary.querySelector("[data-confirm]");
    if (button && state.phase === "ordered") button.textContent = "Pedido confirmado ✓";
  }
  input.disabled = busy;
  sendButton.disabled = busy;
}

// The server keeps the whole conversation; render what's new or changed (by id + rev).
function applyState(next) {
  state = next;
  chat.querySelectorAll(".msg.pending").forEach((n) => n.remove());
  for (const message of next.messages) {
    const existing = chat.querySelector(`.msg[data-id="${message.id}"]`);
    const rev = message.rev || 0;
    if (existing && revisions.get(message.id) !== rev) existing.replaceWith(renderMessage(message));
    else if (!existing) chat.append(renderMessage(message));
    revisions.set(message.id, rev);
  }
  refreshControls();
  scheduleTracking();
}

// ---------------------------------------------------------------- requests

function showTyping() {
  const node = document.getElementById("typing-template").content.firstElementChild.cloneNode(true);
  chat.append(node);
  scrollDown();
  return node;
}

// While a request runs, show what the agent is actually doing (current tool), not filler text.
function watchActivity(typing) {
  const line = typing.querySelector(".status-line");
  const timer = setInterval(async () => {
    try {
      const { activity } = await api("/api/activity");
      line.textContent = activity || "";
    } catch { /* the status line is optional */ }
  }, 700);
  return () => clearInterval(timer);
}

async function run(userText, request, { focus = false } = {}) {
  if (busy) return;
  busy = true;
  if (userText) chat.append(h("div", { class: "msg user pending" }, bubble(userText)));
  refreshControls();
  const typing = showTyping();
  const stopWatching = watchActivity(typing);
  try {
    applyState(await request());
  } catch (error) {
    notice("Não consegui falar com o servidor. Verifique a conexão e tente de novo.");
    console.error(error);
  } finally {
    stopWatching();
    typing.remove();
    busy = false;
    refreshControls();
    scrollDown();
    if (focus) input.focus();
  }
  if (state.needs_resume) resumeAfterLogin();
}

function sendText(text, options) {
  const shown = state.phase === "need_document" ? text.replace(/\S/g, "•") : text;
  run(shown, () => api("/api/chat", { message: text }), options);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  sendText(text, { focus: true });
});

function chooseStore(number, name) {
  run(`Quero o mercado ${name}`, () => api("/api/choose_store", { number }));
}

function chooseAddress(number, label) {
  run(`Estou em ${label}`, () => api("/api/choose_address", { number }));
}

function confirmOrder(documentNumber) {
  run("✅ Confirmar pedido", () => api("/api/confirm", { document: documentNumber || null }));
}

function scheduleTracking() {
  clearTimeout(trackTimer);
  const tracker = [...state.messages].reverse().find((m) => m.kind === "tracker");
  if (!tracker || !tracker.data.poll_after || state.phase !== "ordered") return;
  trackTimer = setTimeout(async () => {
    if (busy) { scheduleTracking(); return; }
    try {
      applyState(await api("/api/track", {}));
    } catch (error) {
      console.error(error);
      scheduleTracking();
    }
  }, tracker.data.poll_after * 1000);
}

document.getElementById("reset").addEventListener("click", () => {
  if (busy) return;
  chat.innerHTML = "";
  revisions.clear();
  run("", () => api("/api/reset", {}));
});

// ---------------------------------------------------------------- iFood login (popup, chat tab stays put)

let login = null; // { popup, timer, started }

function banner(html, actions = []) {
  authBanner.innerHTML = "";
  authBanner.append(h("p", { html }), ...actions);
  authBanner.hidden = false;
}

function stopLogin() {
  if (login) clearInterval(login.timer);
  login = null;
  authBanner.hidden = true;
}

function retryButton(label = "Tentar de novo") {
  return h("button", { type: "button", class: "btn btn-primary btn-small", onclick: startLogin }, label);
}

const FALLBACK_LINK = '<a href="/login" target="_blank" rel="opener">abrir o login em uma nova aba</a>';

function startLogin() {
  stopLogin();
  const w = 480;
  const hgt = 720;
  const left = Math.max(0, window.screenX + (window.outerWidth - w) / 2);
  const top = Math.max(0, window.screenY + (window.outerHeight - hgt) / 2);
  const popup = window.open("/login", "ifood-login", `popup=yes,width=${w},height=${hgt},left=${left},top=${top}`);
  if (!popup) {
    banner(`O navegador bloqueou a janela de login. Permita pop-ups para este site ou ${FALLBACK_LINK}. `
      + "Esta conversa continua aqui.", [retryButton()]);
  } else {
    banner("Faça o login na janela do iFood. Esta conversa fica aberta e continua sozinha quando você terminar.",
      [h("button", { type: "button", class: "btn btn-ghost btn-small", onclick: stopLogin }, "Cancelar")]);
  }
  watchLogin(popup);
}

// Success arrives by postMessage/BroadcastChannel; polling the auth status covers everything else.
function watchLogin(popup) {
  const watch = { popup, started: Date.now(), closedNoticed: false };
  watch.timer = setInterval(async () => {
    try {
      const { connected } = await api("/api/auth_status");
      if (connected) { finishLogin(); return; }
    } catch { /* keep waiting */ }
    if (popup && popup.closed && !watch.closedNoticed) {
      watch.closedNoticed = true;
      banner(`A janela de login foi fechada antes de concluir. Quer tentar de novo? Se preferir, ${FALLBACK_LINK}.`,
        [retryButton()]);
    }
    if (Date.now() - watch.started > 10 * 60 * 1000) stopLogin();
  }, 1500);
  login = watch;
}

function onAuthMessage(data) {
  if (!data || data.type !== "ifood-auth") return;
  if (data.ok) finishLogin();
  else {
    if (login) clearInterval(login.timer);
    login = null;
    banner(`${escapeHtml(data.message || "O login não foi concluído.")} Você pode ${FALLBACK_LINK}.`, [retryButton()]);
  }
}

function finishLogin() {
  stopLogin();
  resumeAfterLogin();
}

function resumeAfterLogin() {
  if (busy) { setTimeout(resumeAfterLogin, 500); return; }
  run("", () => api("/api/resume", {}));
}

window.addEventListener("message", (event) => {
  if (event.origin === location.origin) onAuthMessage(event.data);
});
if ("BroadcastChannel" in window) {
  new BroadcastChannel("ifood-auth").addEventListener("message", (event) => onAuthMessage(event.data));
}

// ---------------------------------------------------------------- start

api("/api/state").then((initial) => {
  applyState(initial);
  scrollDown();
  if (initial.needs_resume) resumeAfterLogin();
}).catch((error) => {
  notice("Não consegui carregar a conversa. Recarregue a página.");
  console.error(error);
});
