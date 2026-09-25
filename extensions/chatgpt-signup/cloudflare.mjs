// Browser equivalent of app/integrations/mail/cloudflare.py and otp.py.
import PostalMime, {addressParser, decodeWords} from './vendor/postal-mime/src/postal-mime.js';

export const MAIL_ORIGIN = 'https://apimail.xiaozhudf2026.foo';
export const RUN_TTL = 10 * 60 * 1000;
const value = (item, ...keys) => {
  for (const key of keys) {
    const text = String(item[key] ?? '').trim();
    if (text && text.toLowerCase() !== 'null') return text;
  }
  return '';
};
export function unwrapItems(payload, depth = 0) {
  if (depth > 3 || !payload) return [];
  if (Array.isArray(payload)) return payload.filter(item => item && typeof item === 'object' && !Array.isArray(item));
  if (typeof payload !== 'object') return [];
  for (const key of ['data', 'results', 'mails', 'items', 'value']) {
    const items = unwrapItems(payload[key], depth + 1);
    if (items.length) return items;
  }
  return [];
}
function addresses(text) {
  const flatten = rows => rows.flatMap(row => row.group ? flatten(row.group) : row.address ? [row.address.toLowerCase()] : []);
  return flatten(addressParser(String(text || '')));
}
function hmeAddress(header, key = 'p') {
  for (const part of String(header || '').split(';')) {
    const match = part.trim().match(new RegExp(`^${key}\\s*=\\s*(.*)$`, 'i'));
    if (match) return addresses(match[1].replace(/^"|"$/g, ''))[0] || '';
  }
  return '';
}
export async function parseMessage(item) {
  const raw = ['raw', 'message'].map(key => value(item, key))
    .find(text => /\n/.test(text) && /^(from|subject|x-icloud-hme):/im.test(text));
  const message = raw ? await PostalMime.parse(raw) : {};
  const header = name => (message.headers || []).find(entry => entry.key === name)?.value || '';
  return {
    id: value(item, 'id', 'mail_id') || message.messageId || '',
    to: hmeAddress(header('x-icloud-hme')) || decodeWords(header('to') || value(item, 'to', 'address', 'recipient', 'rcpt')),
    from: decodeWords(header('from') || value(item, 'from', 'source', 'sender')),
    hmeRecipient: hmeAddress(header('x-icloud-hme')),
    originalSender: hmeAddress(header('x-icloud-hme'), 's'),
    subject: message.subject || decodeWords(value(item, 'subject')),
    body: [message.text, message.html].filter(Boolean).join('\n') || value(item, 'bodyPreview', 'preview', 'text', 'content', 'html', 'body'),
    date: message.date || value(item, 'date', 'created_at', 'received_at'),
  };
}
export function registrationMail(message, email) {
  const senders = addresses(message.from);
  const domain = senders[0]?.split('@').pop();
  const openAI = domain => domain === 'openai.com' || domain?.endsWith('.openai.com');
  const originals = addresses(message.originalSender);
  const forwarded = domain === 'icloud.com' && message.hmeRecipient === email.toLowerCase() &&
    originals.length === 1 && openAI(originals[0].split('@').pop());
  return addresses(message.to).includes(email.toLowerCase()) && senders.length === 1 && (openAI(domain) || forwarded);
}
const HINT = /verification code|one[-\s]?time(?:\s+password|\s+code)?|security code|login code|your code|enter code|temporary (?:verification )?code|otp|验证码|校验码|临时验证码|一次性(?:验证)?码|安全码|动态密码|確認碼|确认码/i;
const CODE_PATTERNS = [
  new RegExp(`(?:${HINT.source})[^\\d]{0,24}(\\d{4,8})`, 'i'),
  /\b(\d{6})\b[^\w]{0,40}(?:is your|verification|one-time|security|验证码|是你的)/i,
  /(?:code|验证码)\s*[:=：]\s*(\d{6})\b/i,
];
const usable = code => /^\d{6}$/.test(code || '') && code !== '000000' ? code : null;
export function extractCode(text) {
  const blob = String(text || '').replace(/<(style|script)\b[^>]*>[\s\S]*?<\/\1>/gi, ' ');
  if (/https:\/\/(?:chatgpt|chat\.openai)\.com\/[^\s"<>]*invite/i.test(blob) && !HINT.test(blob)) return null;
  const isolated = blob.match(/(?:-->|>)\s*(\d{6})\s*(?:<!--|<)/);
  if (usable(isolated?.[1])) return isolated[1];
  const clean = blob.replace(/#[0-9a-f]{3,8}/gi, ' ').replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
  for (const pattern of CODE_PATTERNS) {
    const code = usable(clean.match(pattern)?.[1]);
    if (code) return code;
  }
  return HINT.test(clean) ? usable(clean.match(/(?<![#\w])(\d{6})(?!\w)/)?.[1]) : null;
}
async function fingerprint(message) {
  if (message.id) return `id:${message.id}`;
  const bytes = new TextEncoder().encode(JSON.stringify(message));
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('');
}
export async function fetchMessages(config, email) {
  const url = new URL(config.baseUrl);
  if (url.origin !== MAIL_ORIGIN || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('内置邮箱地址无效。');
  }
  if (!config.adminPassword || !config.address) throw new Error('安装包缺少内置邮箱配置。');
  url.pathname = '/admin/mails';
  url.search = new URLSearchParams({limit: '50', offset: '0', address: config.address});
  let response;
  try {
    response = await fetch(url.href, {headers: {Accept: 'application/json', 'x-admin-auth': config.adminPassword},
      credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(25000)});
  } catch { throw new Error('连接 Cloudflare 邮箱失败，请检查当前网络。'); }
  if (!response.ok) throw new Error(`Cloudflare 邮箱返回 HTTP ${response.status}。`);
  let payload;
  try { payload = await response.json(); } catch { throw new Error('Cloudflare 邮箱响应格式无效。'); }
  const messages = await Promise.all(unwrapItems(payload).map(parseMessage));
  return messages.filter(message => registrationMail(message, email));
}
export async function startMailbox(config, email) {
  const messages = await fetchMessages(config, email);
  return {started: Date.now(), seen: await Promise.all(messages.map(fingerprint)),
    codes: [...new Set(messages.map(message => extractCode(`${message.subject}\n${message.body}`)).filter(Boolean))]};
}
export async function pollMailbox(config, email, baseline, ignoredCodes) {
  const messages = await fetchMessages(config, email);
  const ignored = new Set([...baseline.codes, ...ignoredCodes]);
  for (const message of messages) {
    if (baseline.seen.includes(await fingerprint(message))) continue;
    const date = Date.parse(message.date);
    if (Number.isFinite(date) && date < baseline.started - 30000) continue;
    const code = extractCode(`${message.subject}\n${message.body}`);
    if (code && !ignored.has(code)) return code;
  }
  return null;
}
