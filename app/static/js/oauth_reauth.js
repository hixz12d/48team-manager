let seatOauthState = null;
let seatOauthTimer = null;
let seatOauthJobTimer = null;

function seatOauthLog(text) {
    const box = document.getElementById('seatOauthLog');
    if (box) box.textContent = text || '';
}

function setSeatOauthManualVisible(show) {
    const manual = document.getElementById('seatOauthManual');
    if (manual) manual.hidden = !show;
}

function notifyOauthReauthDone(payload) {
    if (typeof window.oauthReauthOnDone === 'function') {
        window.oauthReauthOnDone(payload);
    }
}

function closeSeatOauth() {
    const modal = document.getElementById('seatOauthModal');
    if (modal) modal.classList.remove('show');
    if (seatOauthTimer) {
        clearInterval(seatOauthTimer);
        seatOauthTimer = null;
    }
    if (seatOauthJobTimer) {
        clearInterval(seatOauthJobTimer);
        seatOauthJobTimer = null;
    }
}

function applySeatOauthMode(data) {
    const help = document.getElementById('seatOauthHelp');
    const cancelBtn = document.getElementById('seatOauthCancel');
    const auto = data && data.mode === 'auto';
    const proxyLabel = data && data.session && data.session.proxy_label;
    setSeatOauthManualVisible(!auto);
    if (cancelBtn) cancelBtn.hidden = !auto;
    if (help) {
        help.textContent = auto
            ? ('iCloud 子号正在服务器上带着代理自动登录、读码、接码' + (proxyLabel ? '（' + proxyLabel + '）' : '') + '。')
            : ((data.message || '会用该号静态 ISP 代理弹出 Chrome。母号请在这个窗口里走 Gmail。') + (proxyLabel ? ' 代理 ' + proxyLabel : ''));
    }
    if (auto && data.job_id) pollSeatOauthJob(data.job_id);
    if (!auto) launchSeatOauthWindow();
}

async function startSeatOauth(teamId, email, forceManual) {
    const modal = document.getElementById('seatOauthModal');
    const title = document.getElementById('seatOauthTitle');
    const callback = document.getElementById('seatOauthCallback');
    if (title) title.textContent = '重新授权 ' + email;
    if (callback) callback.value = '';
    setSeatOauthManualVisible(false);
    const cancelBtn = document.getElementById('seatOauthCancel');
    if (cancelBtn) cancelBtn.hidden = true;
    seatOauthLog('正在准备重新授权，母号会先尝试自动换票…');
    if (modal) modal.classList.add('show');
    try {
        const response = await fetch('/admin/seats/oauth/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                team_id: teamId,
                email: email,
                origin: window.location.origin,
                force_manual: Boolean(forceManual)
            })
        });
        const data = await response.json();
        if (!response.ok || data.success === false) throw new Error(data.error || '启动失败');
        seatOauthState = data;
        seatOauthLog(data.message || '已生成授权链接');
        if (data.mode === 'refreshed') {
            showToast(data.message || '已自动换票并拉回 Team 信息', 'success');
            notifyOauthReauthDone(data);
            return;
        }
        applySeatOauthMode(data);
        pollSeatOauth();
    } catch (error) {
        setSeatOauthManualVisible(true);
        seatOauthLog(error.message);
        showToast(error.message, 'error');
    }
}

function startSeatOauthManual() {
    if (!seatOauthState || !seatOauthState.session) {
        showToast('请先点重新授权', 'error');
        return;
    }
    startSeatOauth(seatOauthState.session.team_id, seatOauthState.session.email, true);
}

function launchSeatOauthWindow() {
    if (!seatOauthState || !seatOauthState.launcher_url) {
        showToast('还没有本机窗口脚本', 'error');
        return;
    }
    if (seatOauthState.session && !seatOauthState.session.proxy_label) {
        showToast('这个号没有静态 ISP 代理，不能弹出授权页', 'error');
        seatOauthLog('缺少代理，拒绝用本机默认浏览器打开，避免登录 IP 对不上。');
        return;
    }
    const link = document.createElement('a');
    link.href = seatOauthState.launcher_url;
    link.download = 'team48-oauth.ps1';
    document.body.appendChild(link);
    link.click();
    link.remove();
    const cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File .\\team48-oauth.ps1';
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).catch(() => {});
    }
    seatOauthLog('已下载带代理的 Chrome 窗口脚本。在下载目录执行：\n' + cmd + '\n会用该号 ISP 代理弹出授权页，localhost 回调不走代理。跑完会自动回写 Team 和 Sub2API。');
}

async function submitSeatOauthCallback() {
    if (!seatOauthState || !seatOauthState.session) {
        showToast('请先点重新授权', 'error');
        return;
    }
    const callback = document.getElementById('seatOauthCallback');
    try {
        const response = await fetch('/admin/seats/oauth/complete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ticket: seatOauthState.session.ticket,
                callback_text: callback ? callback.value : ''
            })
        });
        const data = await response.json();
        if (!response.ok || data.success === false) throw new Error(data.error || '认证失败');
        seatOauthLog(data.message || '认证完成');
        showToast(data.message || '认证完成', data.probe && data.probe.kind === 'phone' ? 'warning' : 'success');
        notifyOauthReauthDone(data);
    } catch (error) {
        seatOauthLog(error.message);
        showToast(error.message, 'error');
    }
}

function pollSeatOauth() {
    if (seatOauthTimer) clearInterval(seatOauthTimer);
    seatOauthTimer = setInterval(async () => {
        if (!seatOauthState || !seatOauthState.session) return;
        try {
            const response = await fetch('/admin/seats/oauth/' + encodeURIComponent(seatOauthState.session.ticket));
            const data = await response.json();
            if (!response.ok || data.success === false) return;
            const session = data.session || {};
            if (session.message) seatOauthLog(session.message);
            if (session.status === 'done') {
                clearInterval(seatOauthTimer);
                seatOauthTimer = null;
                if (seatOauthJobTimer) {
                    clearInterval(seatOauthJobTimer);
                    seatOauthJobTimer = null;
                }
                showToast(session.message || '重新授权完成', 'success');
                notifyOauthReauthDone(session);
            } else if (session.status === 'error') {
                clearInterval(seatOauthTimer);
                seatOauthTimer = null;
                setSeatOauthManualVisible(true);
                const cancelBtn = document.getElementById('seatOauthCancel');
                if (cancelBtn) cancelBtn.hidden = true;
                showToast(session.error || '自动授权失败，可改走手动', 'error');
            }
        } catch (error) {
            console.warn(error);
        }
    }, 2000);
}

function pollSeatOauthJob(jobId) {
    if (seatOauthJobTimer) clearInterval(seatOauthJobTimer);
    const tick = async () => {
        try {
            const response = await fetch('/admin/seats/jobs/' + encodeURIComponent(jobId));
            const data = await response.json();
            if (!response.ok || !data.job) return;
            const job = data.job;
            const lines = (job.log || []).map((item) => '[' + (item.ts || '') + '] ' + (item.message || item.stage || ''));
            if (lines.length) seatOauthLog(lines.join('\n'));
            if (job.status && job.status !== 'running') {
                clearInterval(seatOauthJobTimer);
                seatOauthJobTimer = null;
                const cancelBtn = document.getElementById('seatOauthCancel');
                if (cancelBtn) cancelBtn.hidden = true;
                if (job.status === 'success') {
                    showToast(job.message || '重新授权完成', 'success');
                    notifyOauthReauthDone(job);
                } else if (job.status !== 'cancelled') {
                    setSeatOauthManualVisible(true);
                    showToast(job.error || job.message || '自动授权失败，可改走手动', 'error');
                }
            }
        } catch (error) {
            console.warn(error);
        }
    };
    tick();
    seatOauthJobTimer = setInterval(tick, 2000);
}

async function cancelSeatOauthJob() {
    const jobId = seatOauthState && seatOauthState.job_id;
    if (!jobId) return;
    try {
        const response = await fetch('/admin/seats/jobs/' + encodeURIComponent(jobId) + '/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();
        if (!response.ok || data.success === false) throw new Error(data.error || '停止失败');
        showToast('已请求停止', 'info');
        setSeatOauthManualVisible(true);
    } catch (error) {
        showToast(error.message, 'error');
    }
}
