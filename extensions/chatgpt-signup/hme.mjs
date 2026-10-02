// Reads registration mail from icloud-hme GET /api/inbox (IMAP via the account's app-specific password).
export const HME_ORIGIN = 'https://icloud.xiaozhudf2026.foo';

// iCloud relays rewrite the sender to <local>_at_<domain with _>_<random>@icloud.com.
const openAIRelay = from => /^[^@\s]*_at_(?:[a-z0-9-]+_)*openai_com_[^@\s]*@icloud\.com$/i.test(from);
const openAIDirect = from => /@(?:[a-z0-9-]+\.)*openai\.com$/i.test(from);

export function hmeConfig(config) {
  try {
    const url = new URL(config.baseUrl);
    if (url.origin !== HME_ORIGIN || url.username || url.password || url.pathname !== '/' || url.search || url.hash) return null;
    if (typeof config.token !== 'string' || config.token.length < 16) return null;
    if (typeof config.accountId !== 'string' || !/^acc_[\w-]{4,80}$/.test(config.accountId)) return null;
    return {kind: 'hme', baseUrl: url.origin, token: config.token, accountId: config.accountId};
  } catch { return null; }
}

// Mail on this account is moved to "Deleted Messages" within minutes, so both the inbox/junk ("all") and trash are read.
const FOLDERS = ['all', 'trash'];

async function fetchFolder(config, email, folder) {
  const url = new URL('/api/inbox', config.baseUrl);
  url.search = new URLSearchParams({account_id: config.accountId, alias: email, folder, limit: '20', days: '1'});
  let response;
  try {
    response = await fetch(url.href, {headers: {Accept: 'application/json', 'X-HME-Service-Token': config.token},
      credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(25000)});
  } catch { throw new Error('连接 iCloud HME 读信接口失败，请检查当前网络。'); }
  let payload = null;
  try { payload = await response.json(); } catch { /* handled below */ }
  if (response.status === 401 || response.status === 403) throw new Error('iCloud HME 拒绝了插件令牌，请核对服务 token 后重新打包。');
  if (payload?.code === 'UNSUPPORTED_FILTER') throw new Error('iCloud HME 账号未配置 App 专用密码，无法按邮箱读信。');
  if (!response.ok || !payload?.success) throw new Error(`iCloud HME 读信失败（HTTP ${response.status}）。`);
  return payload.data?.messages || [];
}

export async function fetchHmeMessages(config, email) {
  const target = email.toLowerCase();
  const lists = await Promise.all(FOLDERS.map(folder => fetchFolder(config, email, folder)));
  const seen = new Set();
  return lists.flat()
    .filter(item => String(item.to || '').toLowerCase().split(/\s*,\s*/).includes(target))
    .filter(item => openAIRelay(String(item.from || '')) || openAIDirect(String(item.from || '')))
    .map(item => ({id: `${item.subject}|${item.date}`, subject: String(item.subject || ''), body: String(item.preview || ''), date: String(item.date || '')}))
    // A message moved between folders keeps its subject and date but gets a new folder:uid id.
    .filter(item => !seen.has(item.id) && seen.add(item.id))
    .sort((a, b) => Date.parse(b.date) - Date.parse(a.date));
}
