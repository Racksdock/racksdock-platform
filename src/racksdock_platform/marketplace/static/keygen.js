// Browser-side Ed25519 keypair in OpenSSH formats. The private key never leaves the browser.
function sshString(bytes) { const len = new Uint8Array(4); new DataView(len.buffer).setUint32(0, bytes.length); return concat(len, bytes); }
function concat(...arrs) { const n = arrs.reduce((s, a) => s + a.length, 0); const out = new Uint8Array(n); let o = 0; for (const a of arrs) { out.set(a, o); o += a.length; } return out; }
function b64(bytes) { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); }
const enc = new TextEncoder();
async function generateOpenSshEd25519(comment) {
  const kp = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
  const pubRaw = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  const pkcs8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey));
  const seed = pkcs8.slice(pkcs8.length - 32); // PKCS#8 Ed25519: the seed is the last 32 bytes
  const pubBlob = concat(sshString(enc.encode('ssh-ed25519')), sshString(pubRaw));
  const publicKey = `ssh-ed25519 ${b64(pubBlob)} ${comment}`;
  const check = crypto.getRandomValues(new Uint8Array(4));
  let priv = concat(check, check, sshString(enc.encode('ssh-ed25519')), sshString(pubRaw), sshString(concat(seed, pubRaw)), sshString(enc.encode(comment)));
  let pad = 1; while (priv.length % 8) { priv = concat(priv, new Uint8Array([pad++])); }
  const body = concat(enc.encode('openssh-key-v1\0'), sshString(enc.encode('none')), sshString(enc.encode('none')), sshString(new Uint8Array(0)), new Uint8Array([0, 0, 0, 1]), sshString(pubBlob), sshString(priv));
  const b = b64(body).match(/.{1,70}/g).join('\n');
  return { publicKey, privateKey: `-----BEGIN OPENSSH PRIVATE KEY-----\n${b}\n-----END OPENSSH PRIVATE KEY-----\n` };
}
