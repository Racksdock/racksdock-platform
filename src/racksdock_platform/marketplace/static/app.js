async function api(path, body) {
  const r = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  if (!r.ok) { let m = r.statusText; try { const j = await r.json(); m = j.detail ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : JSON.stringify(j); } catch (e) {} throw new Error(m); }
  return r.json();
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function randomHex(n) { const a = new Uint8Array(n); crypto.getRandomValues(a); return Array.from(a, b => b.toString(16).padStart(2, '0')).join(''); }
function downloadText(name, text) { const blob = new Blob([text], { type: 'application/octet-stream' }); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove(); }
