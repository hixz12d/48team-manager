let seatOauthState = null;
let seatOauthTimer = null;
let seatOauthJobTimer = null;
let seatOauthLaunchTimer = null;

function seatOauthLog(text) {
    const box = document.getElementById('seatOauthLog');
    if (box) box.textContent = text || '';
}

function setSeatOauthManualVisible(show) {
    const manual = document.getElementById('seatOauthManual');
    if (manual) manual.hidden = !show;
}

function setSeatOauthInstallVisible(show) {
    const install = document.getElementById('seatOauthInstall');
    if (install) install.hidden = !show;
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
    if (seatOauthLaunchTimer) {
        clearTimeout(seatOauthLaunchTimer);
        seatOauthLaunchTimer = null;
    }
}

function applySeatOauthMode(data) {
    const help = document.getElementById('seatOauthHelp');
    const cancelBtn = document.getElementById('seatOauthCancel');
    const auto = data && data.mode === 'auto';
    setSeatOauthManualVisible(!auto);
    setSeatOauthInstallVisible(!auto);
    if (cancelBtn) cancelBtn.hidden = !auto;
    if (help) {
        help.hidden = true;
        help.textContent = '';
    }
    if (auto && data.job_id) pollSeatOauthJob(data.job_id);
    if (!auto) seatOauthLog('点「弹出授权窗口」。第一次先在这台 Windows 电脑装一次，不要在 VPS 上装。');
}

async function startSeatOauth(teamId, email, forceManual) {
    const modal = document.getElementById('seatOauthModal');
    const title = document.getElementById('seatOauthTitle');
    const callback = document.getElementById('seatOauthCallback');
    if (title) title.textContent = '重新授权 ' + email;
    if (callback) callback.value = '';
    setSeatOauthManualVisible(false);
    setSeatOauthInstallVisible(false);
    const cancelBtn = document.getElementById('seatOauthCancel');
    if (cancelBtn) cancelBtn.hidden = true;
    seatOauthLog('正在准备重新授权…');
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
        seatOauthLog(data.message || '已准备好');
        if (data.mode === 'refreshed') {
            showToast(data.message || '已重新拉回 Team 信息', 'success');
            notifyOauthReauthDone(data);
            return;
        }
        applySeatOauthMode(data);
        pollSeatOauth();
    } catch (error) {
        setSeatOauthManualVisible(false);
        setSeatOauthInstallVisible(false);
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

function wakeTeam48OauthProtocol(url) {
    const a = document.createElement('a');
    a.href = url;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

function launchSeatOauthWindow() {
    if (!seatOauthState || !seatOauthState.session) {
        showToast('还没准备好授权', 'error');
        return;
    }
    if (seatOauthState.session && !seatOauthState.session.proxy_label) {
        showToast('这个号没有静态 ISP 代理，不能弹出授权页', 'error');
        seatOauthLog('缺少代理，不能弹出授权窗口。');
        return;
    }
    const proto = seatOauthState.protocol_url || (
        'team48-oauth://launch?ticket=' + encodeURIComponent(seatOauthState.session.ticket) +
        '&origin=' + encodeURIComponent(window.location.origin)
    );
    seatOauthState.localLaunched = false;
    wakeTeam48OauthProtocol(proto);
    seatOauthLog('正在唤起本机授权窗口…');
    if (seatOauthLaunchTimer) clearTimeout(seatOauthLaunchTimer);
    seatOauthLaunchTimer = setTimeout(() => {
        if (!seatOauthState || seatOauthState.localLaunched) return;
        setSeatOauthInstallVisible(true);
        seatOauthLog('没唤起本机窗口。在你正在用的这台 Windows 电脑点「安装本机弹出」，不要 SSH 到 VPS 上装。');
    }, 2800);
}

function installSeatOauthProtocol() {
    const url = (seatOauthState && seatOauthState.install_url) || '/admin/seats/oauth/install.ps1';
    const link = document.createElement('a');
    link.href = url;
    link.download = 'install-team48-oauth.ps1';
    document.body.appendChild(link);
    link.click();
    link.remove();
    const cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File .\\install-team48-oauth.ps1';
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).catch(() => {});
    }
    seatOauthLog('已下载。在你这台 Windows 电脑的下载目录执行：\n' + cmd + '\n不要在 VPS 上跑。装好后回到网页再点「弹出授权窗口」，浏览器问是否打开时选允许。');
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
            if (session.message && session.message.indexOf('本机授权窗口已打开') >= 0) {
                seatOauthState.localLaunched = true;
                setSeatOauthInstallVisible(false);
            }
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
