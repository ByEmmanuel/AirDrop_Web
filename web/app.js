'use strict';

const $ = (sel) => document.querySelector(sel);
const ui = {
  file: $('#file'), list: $('#list'), transfers: $('#transfers'), summary: $('#summary'),
  keep: $('#keep'), host: $('#host'), dest: $('#dest'), dot: $('#dot'),
  sheet: $('#sheet'), url: $('#shortcutUrl'), copy: $('#copy'),
};
const ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const ICON_ERR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 6.5v7"/><path d="M12 17.5h.01"/></svg>';

const uploadUrl = new URL('upload', location.href).href;
const queue = [];
let active = null;
let total = 0;
let sent = 0;

/* ---------- estado de la PC ---------- */
async function ping() {
  try {
    const res = await fetch('info', { cache: 'no-store' });
    const info = await res.json();
    ui.host.textContent = info.host;
    ui.dest.textContent = info.dest;
    document.title = `Enviar a ${info.host}`;
    ui.dot.classList.remove('off');
    ui.dot.title = 'Conectado';
  } catch {
    ui.dot.classList.add('off');
    ui.dot.title = 'Sin conexión con la PC';
  }
}
ping();
setInterval(() => { if (!document.hidden) ping(); }, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) ping(); });

/* ---------- elegir o soltar archivos ---------- */
ui.file.addEventListener('change', () => {
  enqueue([...ui.file.files]);
  ui.file.value = '';
});

let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
document.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  document.body.classList.add('dragging');
});
document.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
document.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); }
});
document.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('dragging');
  enqueue([...(e.dataTransfer?.files || [])]);
});

/* ---------- cola de envíos (uno a la vez: aprovecha todo el WiFi) ---------- */
function enqueue(files) {
  if (!files.length) return;
  for (const file of files) {
    queue.push({ file, row: addRow(file) });
    total++;
  }
  ui.transfers.hidden = false;
  pump();
}

function pump() {
  updateSummary();
  if (active) return;
  active = queue.shift() || null;
  ui.keep.hidden = !active;
  if (!active) return;
  upload(active).then(() => { active = null; pump(); });
}

function upload({ file, row }) {
  return new Promise((resolve) => {
    row.li.className = 'item active';
    row.state.innerHTML = '';
    row.sub.textContent = 'Conectando…';
    const started = performance.now();
    const xhr = new XMLHttpRequest();
    xhr.open('POST', 'upload');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = e.loaded / e.total;
      const secs = (performance.now() - started) / 1000;
      row.bar.style.width = `${(pct * 100).toFixed(1)}%`;
      row.sub.textContent = `${Math.round(pct * 100)}% · ${size(e.loaded / Math.max(secs, 0.001))}/s`;
    };
    xhr.onload = () => {
      if (xhr.status === 200) {
        const secs = (performance.now() - started) / 1000;
        row.li.className = 'item ok';
        row.bar.style.width = '100%';
        row.state.innerHTML = ICON_OK;
        row.sub.textContent = `${size(file.size)} · ${secs < 1 ? 'al instante' : `${secs.toFixed(1)} s`}`;
        sent++;
      } else {
        fail(file, row, xhr.responseText.trim() || `Error ${xhr.status}`);
      }
      resolve();
    };
    xhr.onerror = () => { fail(file, row, 'Sin conexión con la PC'); resolve(); };
    xhr.onabort = xhr.onerror;
    xhr.send(file);
  });
}

function fail(file, row, message) {
  row.li.className = 'item err';
  row.state.innerHTML = ICON_ERR;
  row.sub.textContent = `${message} · Toca para reintentar`;
  row.bar.style.width = '0';
  row.li.addEventListener('click', () => {
    row.li.className = 'item';
    row.state.innerHTML = '';
    row.sub.textContent = `En cola · ${size(file.size)}`;
    queue.push({ file, row });
    pump();
  }, { once: true });
}

/* ---------- interfaz ---------- */
function addRow(file) {
  const li = document.createElement('li');
  li.className = 'item';
  li.innerHTML = '<div class="thumb"></div><div class="meta"><div class="name"></div><div class="sub"></div><div class="bar"><i></i></div></div><div class="state"></div>';
  li.querySelector('.name').textContent = file.name;
  const row = { li, sub: li.querySelector('.sub'), bar: li.querySelector('.bar i'), state: li.querySelector('.state') };
  row.sub.textContent = `En cola · ${size(file.size)}`;

  const thumb = li.querySelector('.thumb');
  thumb.textContent = extension(file.name);
  if (file.type.startsWith('image/')) {
    const img = new Image();
    img.alt = '';
    img.onload = () => { thumb.textContent = ''; thumb.append(img); URL.revokeObjectURL(img.src); };
    img.onerror = () => URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(file);
  }
  ui.list.prepend(li);
  return row;
}

function updateSummary() {
  const pending = queue.length + (active ? 1 : 0);
  ui.summary.textContent = pending ? `${sent} de ${total}` : `${sent} ${sent === 1 ? 'enviado' : 'enviados'}`;
}

function extension(name) {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1, dot + 5).toUpperCase() : 'FILE';
}

function size(bytes) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${i ? bytes.toFixed(1) : Math.round(bytes)} ${units[i]}`;
}

window.addEventListener('beforeunload', (e) => {
  if (active || queue.length) { e.preventDefault(); e.returnValue = ''; }
});

/* ---------- hoja del atajo de Compartir ---------- */
ui.url.textContent = uploadUrl;
$('#howto').addEventListener('click', () => ui.sheet.showModal());
$('#close').addEventListener('click', () => ui.sheet.close());
ui.sheet.addEventListener('click', (e) => { if (e.target === ui.sheet) ui.sheet.close(); });

ui.copy.addEventListener('click', async () => {
  const ok = await copyText(uploadUrl);
  ui.copy.textContent = ok ? 'Copied ✓' : 'Press and hold';
  setTimeout(() => { ui.copy.textContent = 'Copy'; }, 1800);
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text); // solo existe en HTTPS
    return true;
  } catch {
    // En HTTP: textarea temporal dentro del diálogo (lo de afuera queda inerte)
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.readOnly = true;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;font-size:16px';
    ui.sheet.append(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* sin soporte */ }
    ta.remove();
    return ok;
  }
}
