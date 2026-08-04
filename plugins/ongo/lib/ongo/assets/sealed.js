const CAPABILITY_PREFIX = "ongo-key-v1.";
const STORAGE_KEY = "ongo-sealed-keys-v1";
const AAD_PREFIX = "ongo-sealed-v1:";
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", {fatal: true});

let manifest = null;
let envelopeCache = new Map();
let activeObjectUrl = null;
let memoryKnownKeys = [];
let memoryTheme = null;
let storageAvailable = true;

function hasWebCrypto() {
  return Boolean(globalThis.crypto && crypto.subtle);
}

function storageGet(name) {
  if (!storageAvailable) return null;
  try {
    return localStorage.getItem(name);
  } catch (_error) {
    storageAvailable = false;
    return null;
  }
}

function storageSet(name, value) {
  if (!storageAvailable) return false;
  try {
    localStorage.setItem(name, value);
    return true;
  } catch (_error) {
    storageAvailable = false;
    return false;
  }
}

function bytesToBase64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlToBytes(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error("The key or ciphertext is not valid base64url.");
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") +
    "=".repeat((4 - value.length % 4) % 4);
  let binary;
  try {
    binary = atob(padded);
  } catch (_error) {
    throw new Error("The key or ciphertext is not valid base64url.");
  }
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

export function normalizeCapability(value) {
  let normalized = String(value || "").trim();
  if (normalized.startsWith(CAPABILITY_PREFIX)) {
    normalized = normalized.slice(CAPABILITY_PREFIX.length);
  }
  const bytes = base64urlToBytes(normalized);
  if (bytes.length !== 32) throw new Error("An Ongo access key must be 256 bits.");
  return CAPABILITY_PREFIX + bytesToBase64url(bytes);
}

async function importCapability(value) {
  const normalized = normalizeCapability(value);
  const raw = base64urlToBytes(normalized.slice(CAPABILITY_PREFIX.length));
  return crypto.subtle.importKey("raw", raw, {name: "AES-GCM"}, false, ["decrypt"]);
}

function validatePayload(payload, envelope) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
      payload.schema_version !== 1 || payload.resource_id !== envelope.resource_id ||
      payload.collection !== envelope.collection || typeof payload.title !== "string" ||
      typeof payload.date !== "string" || !Array.isArray(payload.tags) ||
      !payload.tags.every(tag => typeof tag === "string") ||
      !["html", "pdf"].includes(payload.format)) {
    throw new Error("Decrypted resource has an invalid schema.");
  }
  if (payload.format === "html" && typeof payload.html !== "string") {
    throw new Error("Decrypted HTML resource has an invalid schema.");
  }
  if (payload.format === "pdf" &&
      (typeof payload.data_base64 !== "string" || typeof payload.filename !== "string")) {
    throw new Error("Decrypted PDF resource has an invalid schema.");
  }
}

export async function decryptEnvelope(envelope, knownKeys, expected) {
  if (!envelope || envelope.schema_version !== 1 ||
      typeof envelope.resource_id !== "string" ||
      typeof envelope.collection !== "string") {
    throw new Error("Encrypted resource has an invalid schema.");
  }
  if (!expected || envelope.resource_id !== expected.resource_id ||
      envelope.collection !== expected.collection) {
    throw new Error("Encrypted resource does not match its site manifest entry.");
  }
  const hasPublic = Object.prototype.hasOwnProperty.call(envelope, "public");
  const hasVariants = Array.isArray(envelope.variants);
  if (hasPublic === hasVariants) throw new Error("Resource access envelope is ambiguous.");
  if (hasPublic) {
    validatePayload(envelope.public, envelope);
    return {payload: envelope.public, unlockedBy: [], public: true};
  }
  const aad = encoder.encode(AAD_PREFIX + envelope.resource_id);
  let plaintext = null;
  let payload = null;
  const unlockedBy = [];
  for (const known of knownKeys) {
    let key;
    try {
      key = await importCapability(known.capability);
    } catch (_error) {
      continue;
    }
    let succeeded = false;
    for (const variant of envelope.variants) {
      if (!variant || typeof variant.nonce !== "string" ||
          typeof variant.ciphertext !== "string") continue;
      try {
        const clear = await crypto.subtle.decrypt(
          {
            name: "AES-GCM",
            iv: base64urlToBytes(variant.nonce),
            additionalData: aad,
            tagLength: 128,
          },
          key,
          base64urlToBytes(variant.ciphertext),
        );
        const candidate = decoder.decode(clear);
        const parsed = JSON.parse(candidate);
        validatePayload(parsed, envelope);
        if (plaintext !== null && plaintext !== candidate) {
          throw new Error("Access keys decrypted conflicting resource versions.");
        }
        plaintext = candidate;
        payload = parsed;
        succeeded = true;
      } catch (error) {
        if (error.message === "Access keys decrypted conflicting resource versions.") {
          throw error;
        }
      }
    }
    if (succeeded && !unlockedBy.includes(known.label)) unlockedBy.push(known.label);
  }
  return payload === null ? null : {payload, unlockedBy, public: false};
}

export function loadKnownKeys() {
  let value = memoryKnownKeys;
  const stored = storageGet(STORAGE_KEY);
  if (stored !== null) {
    try {
      value = JSON.parse(stored);
    } catch (_error) {
      value = [];
    }
  }
  if (!Array.isArray(value)) return [];
  const keys = [];
  const seen = new Set();
  for (const entry of value) {
    if (!entry || typeof entry.label !== "string" ||
        typeof entry.capability !== "string") continue;
    try {
      const capability = normalizeCapability(entry.capability);
      if (!entry.label.trim() || seen.has(capability)) continue;
      keys.push({label: entry.label.trim(), capability});
      seen.add(capability);
    } catch (_error) {
      continue;
    }
  }
  memoryKnownKeys = keys;
  return keys;
}

export function saveKnownKeys(keys) {
  memoryKnownKeys = keys.map(key => ({...key}));
  return storageSet(STORAGE_KEY, JSON.stringify(memoryKnownKeys));
}

async function capabilityFingerprint(capability) {
  if (!hasWebCrypto()) return "crypto unavailable";
  const normalized = normalizeCapability(capability);
  const raw = base64urlToBytes(normalized.slice(CAPABILITY_PREFIX.length));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", raw));
  return Array.from(digest.slice(0, 6), byte => byte.toString(16).padStart(2, "0")).join("");
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function keyBadges(labels) {
  const container = element("span", "key-badges");
  for (const label of labels) container.append(element("span", "key-badge", label));
  return container;
}

function tagsNode(tags) {
  const container = element("span", "resource-tags");
  for (const tag of tags) container.append(element("span", "resource-tag", tag));
  return container;
}

function safeHref(value) {
  if (typeof value !== "string" || value !== value.trim() ||
      /[\\\u0000-\u0020\u007f]/u.test(value) || value.startsWith("//")) return false;
  const scheme = value.match(/^([a-z][a-z0-9+.-]*):/iu);
  return scheme === null || ["https", "http", "mailto"].includes(scheme[1].toLowerCase());
}

function safeMediaSrc(value, allowRemoteImages) {
  if (/^data:image\/(?:avif|gif|jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/iu.test(value)) {
    return true;
  }
  if (!safeHref(value)) return false;
  const scheme = value.match(/^([a-z][a-z0-9+.-]*):/iu);
  return scheme === null || (allowRemoteImages &&
    ["https", "http"].includes(scheme[1].toLowerCase()));
}

function sanitizedFragment(htmlText, allowRemoteImages = false) {
  const template = document.createElement("template");
  template.innerHTML = htmlText;
  const allowedTags = new Set([
    "A", "ABBR", "ARTICLE", "B", "BLOCKQUOTE", "BR", "CODE", "DD", "DEL",
    "DETAILS", "DIV", "DL", "DT", "EM", "FIGCAPTION", "FIGURE", "H1", "H2",
    "H3", "H4", "H5", "H6", "HR", "I", "IMG", "KBD", "LI", "MARK", "NAV",
    "OL", "P", "PRE", "S", "SECTION", "SMALL", "SPAN", "STRONG", "SUB",
    "SUMMARY", "SUP", "TABLE", "TBODY", "TD", "TFOOT", "TH", "THEAD", "TIME",
    "TR", "U", "UL",
  ]);
  const globalAttributes = new Set([
    "aria-hidden", "aria-label", "class", "id", "role", "title",
  ]);
  const tagAttributes = {
    A: new Set(["href", "rel", "target"]),
    BLOCKQUOTE: new Set(["cite"]),
    DETAILS: new Set(["open"]),
    IMG: new Set(["alt", "height", "loading", "src", "width"]),
    LI: new Set(["value"]),
    OL: new Set(["reversed", "start"]),
    DIV: new Set(["data-display", "data-tex"]),
    SPAN: new Set(["data-display", "data-tex"]),
    TD: new Set(["colspan", "rowspan"]),
    TH: new Set(["colspan", "rowspan", "scope"]),
    TIME: new Set(["datetime"]),
  };
  const nodes = Array.from(template.content.querySelectorAll("*"));
  for (const node of nodes) {
    if (!allowedTags.has(node.tagName)) {
      node.replaceWith(document.createTextNode(node.textContent || ""));
      continue;
    }
    for (const attribute of Array.from(node.attributes)) {
      const specific = tagAttributes[node.tagName] || new Set();
      if (!globalAttributes.has(attribute.name) && !specific.has(attribute.name)) {
        node.removeAttribute(attribute.name);
      }
    }
    if (node.tagName === "A") {
      const href = node.getAttribute("href") || "";
      if (!safeHref(href)) node.removeAttribute("href");
      if (node.getAttribute("target") !== "_blank") node.removeAttribute("target");
      if (!node.hasAttribute("href")) node.removeAttribute("target");
      if (node.hasAttribute("target")) node.setAttribute("rel", "noopener noreferrer");
      else node.removeAttribute("rel");
    } else if (node.tagName === "IMG") {
      const src = node.getAttribute("src") || "";
      if (!safeMediaSrc(src, allowRemoteImages)) node.removeAttribute("src");
      node.removeAttribute("href");
      node.removeAttribute("target");
      node.removeAttribute("rel");
    } else {
      node.removeAttribute("href");
      node.removeAttribute("target");
      node.removeAttribute("rel");
    }
  }
  return template.content;
}

export async function fetchEnvelope(entry) {
  if (envelopeCache.has(entry.resource_id)) {
    return envelopeCache.get(entry.resource_id);
  }
  const prefix = document.body.dataset.assetPrefix || "";
  const request = fetch(prefix + entry.envelope).then(response => {
    if (!response.ok) throw new Error(`Could not load encrypted resource (${response.status}).`);
    return response.json();
  });
  envelopeCache.set(entry.resource_id, request);
  try {
    return await request;
  } catch (error) {
    if (envelopeCache.get(entry.resource_id) === request) {
      envelopeCache.delete(entry.resource_id);
    }
    throw error;
  }
}

function collectionLabel(collection) {
  return collection === "experiment" ? "experiment" :
    collection === "digest" ? "digest" : "article";
}

async function decryptedEntry(entry, keys) {
  const envelope = await fetchEnvelope(entry);
  return decryptEnvelope(envelope, keys, entry);
}

async function renderList(keys) {
  const root = document.getElementById("ongo-content");
  root.replaceChildren(element("p", "intro", "Decrypting locally…"));
  const page = document.body.dataset.page;
  const selected = manifest.resources.filter(entry =>
    page === "index" || entry.collection === page.slice(0, -1)
  );
  const results = await Promise.all(selected.map(async entry => {
    try {
      return {entry, decrypted: await decryptedEntry(entry, keys), error: null};
    } catch (error) {
      return {entry, decrypted: null, error};
    }
  }));
  const fragment = document.createDocumentFragment();
  const intro = element("p", "intro",
    `${selected.length} resource${selected.length === 1 ? "" : "s"}. ` +
    (hasWebCrypto()
      ? "Public content is shown immediately; accessible encrypted content is decrypted only in this browser."
      : "Public content is shown immediately; encrypted content requires Web Crypto over HTTPS."));
  fragment.append(intro);
  const list = element("ul", "index sealed-index");
  for (const result of results) {
    const row = element("li", "sealed-row");
    if (result.error) {
      row.append(element("div", "locked-resource error-resource", "Encrypted resource (invalid or corrupted)"));
    } else if (!result.decrypted) {
      row.append(element("div", "locked-resource", `Encrypted ${collectionLabel(result.entry.collection)}`));
    } else {
      const link = element("a", "resource-link");
      link.href = result.entry.page;
      const main = element("span", "resource-main");
      main.append(element("span", "ititle", result.decrypted.payload.title));
      if (result.decrypted.payload.tags.length) main.append(tagsNode(result.decrypted.payload.tags));
      main.append(keyBadges(result.decrypted.unlockedBy));
      link.append(main, element("span", "idate", result.decrypted.payload.date));
      row.append(link);
    }
    list.append(row);
  }
  fragment.append(list);
  root.replaceChildren(fragment);
  window.__ONGO_ACCESSIBLE_ITEMS__ = results
    .filter(result => result.decrypted)
    .map(result => result.entry.page);
}

function renderPdf(payload, unlockedBy, root) {
  let binary;
  try {
    binary = atob(payload.data_base64);
  } catch (_error) {
    throw new Error("Decrypted PDF has invalid base64 content.");
  }
  const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
  if (activeObjectUrl) URL.revokeObjectURL(activeObjectUrl);
  activeObjectUrl = URL.createObjectURL(new Blob([bytes], {type: "application/pdf"}));
  const article = element("article");
  article.append(element("h1", "page-title", payload.title));
  article.append(keyBadges(unlockedBy));
  article.append(element("p", "byline", "Published PDF document"));
  const download = element("a", "", "↓ Download PDF");
  download.href = activeObjectUrl;
  download.download = payload.filename;
  const paragraph = element("p");
  paragraph.append(download);
  const frame = element("iframe", "embed");
  frame.title = payload.title;
  frame.src = activeObjectUrl;
  article.append(paragraph, frame);
  root.replaceChildren(article);
}

async function renderItem(keys) {
  const root = document.getElementById("ongo-content");
  root.replaceChildren(element("p", "intro", "Decrypting locally…"));
  const resourceId = document.body.dataset.resource;
  const entry = manifest.resources.find(item => item.resource_id === resourceId);
  if (!entry) {
    root.replaceChildren(element("p", "error-resource", "Encrypted resource is not present in this build."));
    return;
  }
  let decrypted;
  try {
    decrypted = await decryptedEntry(entry, keys);
  } catch (_error) {
    root.replaceChildren(element("p", "error-resource", "Encrypted resource is invalid or corrupted."));
    return;
  }
  if (!decrypted) {
    root.replaceChildren(element("p", "locked-resource", `Encrypted ${collectionLabel(entry.collection)}`));
    return;
  }
  const payload = decrypted.payload;
  document.title = `${payload.title} — ${manifest.site_title}`;
  if (payload.format === "pdf") {
    renderPdf(payload, decrypted.unlockedBy, root);
    return;
  }
  const wrapper = element("div");
  const access = element("div", "item-access");
  access.append(keyBadges(decrypted.unlockedBy));
  if (payload.tags.length) access.append(tagsNode(payload.tags));
  wrapper.append(access, sanitizedFragment(payload.html, decrypted.public));
  root.replaceChildren(wrapper);
  if (typeof window.__ongoRenderMath === "function") window.__ongoRenderMath();
}

async function populateAccessibleItems(keys) {
  const prefix = document.body.dataset.assetPrefix || "";
  const results = await Promise.all(manifest.resources.map(async entry => {
    try {
      return await decryptedEntry(entry, keys) ? prefix + entry.page : null;
    } catch (error) {
      console.warn(`Could not classify accessible resource ${entry.resource_id}:`, error);
      return null;
    }
  }));
  window.__ONGO_ACCESSIBLE_ITEMS__ = results.filter(Boolean);
}

async function renderKeyring() {
  const list = document.getElementById("key-list");
  list.replaceChildren();
  const keys = loadKnownKeys();
  if (!keys.length) {
    list.append(element("p", "key-empty", "No access keys registered in this browser."));
    return;
  }
  for (const [index, key] of keys.entries()) {
    const row = element("div", "key-row");
    const details = element("span", "key-details");
    const fingerprint = await capabilityFingerprint(key.capability);
    details.append(element("strong", "", key.label), element("code", "", fingerprint));
    const remove = element("button", "key-remove", "Remove");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      const updated = loadKnownKeys();
      updated.splice(index, 1);
      const persisted = saveKnownKeys(updated);
      document.getElementById("key-message").textContent = persisted
        ? "Key removed."
        : "Key removed for this page session; browser storage is unavailable.";
      await refresh();
    });
    row.append(details, remove);
    list.append(row);
  }
}

async function refresh() {
  const keys = loadKnownKeys();
  await renderKeyring();
  if (document.body.dataset.page === "item") {
    await Promise.all([renderItem(keys), populateAccessibleItems(keys)]);
  }
  else await renderList(keys);
}

function bindControls() {
  const toggle = document.getElementById("keyring-toggle");
  const panel = document.getElementById("keyring-panel");
  toggle.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
  });
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || "light";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    memoryTheme = next;
    storageSet("ongo-theme", next);
  });
  document.getElementById("rand-article").addEventListener("click", () => {
    const choices = window.__ONGO_ACCESSIBLE_ITEMS__ || [];
    if (choices.length) window.location.href = choices[Math.floor(Math.random() * choices.length)];
  });
  document.getElementById("key-form").addEventListener("submit", async event => {
    event.preventDefault();
    const message = document.getElementById("key-message");
    const labelInput = document.getElementById("key-label");
    const capabilityInput = document.getElementById("key-capability");
    try {
      const label = labelInput.value.trim();
      if (!label) throw new Error("Enter a local label for this key.");
      const capability = normalizeCapability(capabilityInput.value);
      const keys = loadKnownKeys();
      const existing = keys.find(item => item.capability === capability);
      if (existing) existing.label = label;
      else keys.push({label, capability});
      const persisted = saveKnownKeys(keys);
      labelInput.value = "";
      capabilityInput.value = "";
      message.textContent = persisted
        ? "Key registered locally."
        : "Key registered for this page session; browser storage is unavailable.";
      await refresh();
    } catch (error) {
      message.textContent = error.message;
    }
  });
}

async function init() {
  const theme = storageGet("ongo-theme") || memoryTheme;
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  bindControls();
  const prefix = document.body.dataset.assetPrefix || "";
  const response = await fetch(prefix + "assets/ongo-sealed.json");
  if (!response.ok) throw new Error(`Could not load the site manifest (${response.status}).`);
  manifest = await response.json();
  if (!manifest || manifest.schema_version !== 1 || !Array.isArray(manifest.resources)) {
    throw new Error("Site manifest has an invalid schema.");
  }
  await refresh();
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    init().catch(error => {
      const root = document.getElementById("ongo-content");
      if (root) root.textContent = `Site failed: ${error.message}`;
    });
  });
}
