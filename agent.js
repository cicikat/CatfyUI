(() => {
    const labels = {
        planning: '正在拆解指令', approval: '待确认计划', dispatch: '准备入队', running: '巡检中',
        paused: '巡检已暂停', blocked: '需要处理', stopped: '已停止', completed: '已覆盖全部镜头',
        attention: '部分镜头待处理'
    };
    const formatTime = value => new Date(value * 1000).toLocaleString('zh-CN', {hour12: false});
    let busy = false;

    function fillConfig(config) {
        $('agentUrl').value = config.url;
        $('agentKey').value = '';
        $('agentKey').placeholder = config.has_key ? '已保存，留空沿用' : '输入 API Key（本地服务可留空）';
        $('agentModel').value = config.model;
        $('agentVision').value = config.vision;
        $('agentModels').innerHTML = config.models.map(model => `<option value="${esc(model)}"></option>`).join('');
    }

    function renderRun(run) {
        const shots = run.shots.map(shot => {
            const slots = shot.slots.map(slot => {
                const id = slot.jobs.at(-1);
                const job = state?.jobs.find(item => item.id === id);
                const review = slot.reviews.at(-1);
                const status = {pass: '抽帧通过', fail: '抽帧未通过', uncertain: '需人工确认',
                    completed: '技术生成完成', cancelled: '取消', interrupted: '中断', failed: '失败'}[slot.result];
                const file = job?.output ? ` · <a href="/media/job/${esc(id)}/video" target="_blank" rel="noopener">预览 MP4 ↗</a>` : '';
                return `<li>${slot.stage === 'formal' ? '正式' : '候选'} · ${esc(status || job?.stage || id)}${file}
                    ${review ? `<div class="agent-muted">${esc(review.reason)}</div>` : ''}
                    <small>${slot.jobs.map(esc).join(' → ')}</small></li>`;
            }).join('');
            return `<details><summary>${esc(shot.options.title || shot.options.prompt.slice(0, 32))} ·
                ${esc(shot.options.workflow)} · ${shot.options.seconds}s · ${shot.options.candidates} 条候选</summary>
                <p class="agent-pre">${esc(shot.options.prompt)}</p><ul>${slots}</ul></details>`;
        }).join('');
        const actions = [];
        if (run.status === 'approval') actions.push(['approve', '确认计划并入队']);
        if (['planning', 'dispatch', 'running'].includes(run.status)) actions.push(['pause', '暂停巡检']);
        if (['paused', 'blocked'].includes(run.status)) actions.push(['resume', '继续']);
        if (run.status === 'running') actions.push(['check', '立即检查']);
        if (!['completed', 'attention', 'stopped'].includes(run.status)) actions.push(['stop', '停止并取消本次任务']);
        return `<article class="composer agent-run">
            <div class="agent-run-head"><strong>${esc(labels[run.status] || run.status)}</strong>
            <small>${formatTime(run.created)} · ${esc(run.model)}</small></div>
            <p class="agent-pre">${esc(run.instruction)}</p>
            <p class="agent-muted">已提交 ${run.used} / ${run.budget} 条 · ${run.minutes} 分钟巡检
            ${run.status === 'running' ? ' · 下次 ' + formatTime(run.next) : ''}
            · ${run.review ? '视觉模型抽帧审查' : '技术状态检查，成片待人工审片'}</p>
            ${run.error ? `<p class="warning agent-pre">${esc(run.error)}</p>` : ''}
            <div class="agent-controls">${actions.map(([action, text]) =>
                `<button class="secondary" data-agent-action="${action}" data-run="${run.id}">${text}</button>`).join('')}</div>
            ${shots ? `<details><summary>分镜计划与生成结果 (${run.shots.length})</summary>${shots}</details>` : ''}
            ${run.events.length ? `<details><summary>运行记录</summary><ul>${run.events.map(event =>
                `<li><small>${formatTime(event.at)}</small> ${esc(event.text)}</li>`).join('')}</ul></details>` : ''}
            </article>`;
    }

    let signature = '';
    async function refreshAgent(config = false) {
        const response = await api('/api/agent');
        if (config) fillConfig(response.config);
        const next = response.runs.map(renderRun).join('');
        if (next === signature) return;
        const root = $('agentRuns');
        const open = [...root.querySelectorAll('details')].flatMap((node, index) => node.open ? [index] : []);
        signature = next;
        root.innerHTML = next || '<p class="agent-muted">还没有 Agent 运行。</p>';
        root.querySelectorAll('details').forEach((node, index) => { node.open = open.includes(index); });
    }

    async function perform(button, operation) {
        if (busy) return;
        busy = true;
        button.disabled = true;
        try { await operation(); }
        catch (error) { toast(error.message, true); }
        finally { busy = false; button.disabled = false; }
    }

    for (const action of ['discover', 'connect']) {
        const button = $('agent-' + action);
        button.onclick = () => perform(button, async () => {
            $('agentConnection').textContent = '正在测试 API…';
            try {
                const result = await api('/api/agent/' + action, {
                    url: $('agentUrl').value, key: $('agentKey').value,
                    model: $('agentModel').value, vision: $('agentVision').value,
                    forget_key: $('agentForget').checked
                });
                $('agentForget').checked = false;
                await refreshAgent(true);
                $('agentConnection').textContent = result.message;
            } catch (error) {
                $('agentConnection').textContent = error.message;
                throw error;
            }
        });
    }
    $('agentStart').onclick = () => perform($('agentStart'), async () => {
        const file = $('agentFile').files[0];
        let token;
        if (file) {
            const response = await fetch('/api/upload?name=' + encodeURIComponent(file.name), {method: 'POST', body: file});
            const uploaded = await response.json();
            if (!response.ok) throw Error(uploaded.error);
            token = uploaded.token;
        }
        await api('/api/agent/start', {
            instruction: $('agentInstruction').value, base: loaded ? collect() : {},
            minutes: $('agentInterval').value, budget: $('agentBudget').value,
            retries: $('agentRetries').value, auto: $('agentAuto').checked,
            review: $('agentReview').checked, promotion: $('agentPromotion').value,
            use_library: $('agentLibrary').checked, token
        });
        toast('Agent 已在后台开始规划。');
        await refreshAgent();
    });
    $('agentRuns').onclick = event => {
        const button = event.target.closest('[data-agent-action]');
        if (!button) return;
        perform(button, async () => {
            await api('/api/agent/' + button.dataset.agentAction, {id: button.dataset.run});
            await refreshAgent();
        });
    };
    $('agentReview').onchange = () => {
        $('agentPromotion').disabled = !$('agentReview').checked;
        if ($('agentPromotion').disabled) $('agentPromotion').value = '';
    };
    document.querySelector('[data-page="agent"]').addEventListener('click', () =>
        refreshAgent(true).catch(error => toast(error.message, true)), {once: true});
    setInterval(() => { if (page === 'agent') refreshAgent().catch(() => {}); }, 3000);
})();
