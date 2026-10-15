// Small helpers shared by all pages.
// h() builds DOM nodes with textContent only, so nothing a user typed can ever
// be interpreted as HTML (no cross-site scripting through lead descriptions).
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Replace an element's contents. Accepts nested arrays and skips null/false,
// so pages can write `cond ? node : null` inline.
export function fill(el, ...children) {
  const nodes = children
    .flat(Infinity)
    .filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
  el.replaceChildren(...nodes);
}

export function money(cents) {
  if (cents === null || cents === undefined) return "-";
  return cents === 0 ? "$0 (included)" : `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
}

export function phone(digits) {
  const d = String(digits ?? "");
  return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : d;
}

export function label(value) {
  return String(value ?? "-").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function when(iso) {
  return iso ? new Date(iso).toLocaleString() : "-";
}

const TONES = {
  assigned: "ok", accepted: "ok",
  offered: "info", pending: "info", queued: "",
  unsold: "warn", expired: "warn", skipped: "warn", lost: "",
  rejected: "bad", unserviceable: "warn", declined: "bad",
  A: "ok", B: "info", C: "warn",
};

export function pill(value) {
  return h("span", { class: `pill ${TONES[value] ?? ""}` }, label(value));
}

export async function api(path, { method = "GET", body, headers = {} } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => `${d.loc.slice(-1)[0]}: ${d.msg.replace(/^Value error, /, "")}`).join("; ")
      : data.detail;
    const err = new Error(detail || `Request failed (${res.status})`);
    err.status = res.status;
    err.fields = Array.isArray(data.detail) ? data.detail : [];  // for per-field messages
    throw err;
  }
  return data;
}
