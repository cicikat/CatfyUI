(() => {
    const root = $('filmProject');
    const picker = $('filmPicker');
    let project;
    let signature = '';
    let busy = false;
    const labels = {
        planning: '正在规划', awaiting_approval: '待批准', dispatching: '准备入队',
        generating: '等待候选', clip_review: '单镜审查', continuity_review: '导演复盘',
        paused: '已暂停', blocked: '遇到故障', stopped: '已停止',
        assembling: '正在剪辑', final_review: '正在检查成片', completed: '成片已完成',
        needs_attention: '待人工确认'
    };
    const budgetLabels = {
        generations: '生成', calls: '模型调用', rounds: '复盘',
        inserts: '补镜', revisions: '单镜返修', assemblies: '合成'
    };
    const button = (action, text) => `<button type="button" class="secondary" data-film-action="${action}">${text}</button>`;
    const link = (url, text) => `<a href="${esc(url)}" target="_blank" rel="noopener">${text}</a>`;
    const baseUrl = () => '/api/films/' + picker.value;

    async function refresh(force = false) {
        const projects = await api('/api/films');
        const selected = picker.value || projects[0]?.id;
        picker.innerHTML = projects.map(item =>
            `<option value="${item.id}">${esc(item.brief.slice(0, 40))} · ${labels[item.status]}</option>`).join('');
        if (!selected) return;
        picker.value = selected;
        const next = await api(baseUrl());
        if (picker.value !== next.id) return;
        const serialized = JSON.stringify(next);
        if (!force && (serialized === signature || root.contains(document.activeElement))) return;
        project = next;
        signature = serialized;
        render();
    }

    function render() {
        const p = project;
        const states = ['planning', 'dispatching', 'generating', 'clip_review', 'continuity_review', 'assembling', 'final_review'];
        const actions = [
            p.status === 'awaiting_approval' && button('approve', '批准并继续'),
            p.pending && button('reject', '拒绝提议'),
            states.includes(p.status) && button('pause', '暂停导演'),
            ['paused', 'blocked'].includes(p.status) && button('resume', '继续'),
            !['stopped', 'completed'].includes(p.status) && button('stop', '停止导演'),
            !['stopped', 'completed'].includes(p.status) && button('cancel', '停止并取消项目任务'),
            ...renderActions(p)
        ].filter(Boolean).join('');
        const open = new Set([...root.querySelectorAll('details[open][id]')].map(node => node.id));
        root.innerHTML = `
            <div class="agent-run-head"><strong>${labels[p.status]} · 分镜 v${p.revision}</strong>
            <small>${esc(p.model)} · ${p.target} 秒目标</small></div>
            <p class="agent-pre">${esc(p.brief)}</p>
            <p class="agent-muted">${Object.entries(budgetLabels).map(([key, name]) =>
                key === 'revisions' ? `${name}上限 ${p.budgets[key]}` :
                    `${name} ${p.used[key]}/${p.budgets[key]}`).join(' · ')}</p>
            ${p.error ? `<p class="warning agent-pre">${esc(p.error)}</p>` : ''}
            ${p.pending ? `<details open id="filmProposal"><summary>待批准 · ${esc(p.pending.analysis)}</summary>
                <ul>${p.pending.operations.map(op => `<li>${describeOperation(op)}</li>`).join('')}</ul></details>` : ''}
            <div class="agent-controls">${actions}</div>
            ${renderProducts(p)}
            ${p.transition_reports?.length ? `<details id="filmContinuity"><summary>相邻镜头复盘</summary>
                <ul>${p.transition_reports.map(r => `<li>${r.shots.join(' → ')} · ${esc(r.reason)}</li>`).join('')}</ul>
                </details>` : ''}
            ${p.revision ? `<details id="filmBible"><summary>故事设定</summary>
                <dl>${Object.entries(p.story_bible).map(([key, value]) => `<dt>${bibleNames[key]}</dt><dd>${esc(value)}</dd>`).join('')}</dl>
                </details>` : ''}
            ${p.order.map(sid => shotCard(p.shots[sid], p)).join('')}
            ${p.revision ? `<details id="filmHistory"><summary>计划版本和决策日志</summary>
                <label>历史版本 <select id="filmRevision"><option value="">选择版本</option>${Array.from({length:p.revision}, (_, n) =>
                    `<option>${n+1}</option>`).join('')}</select></label><pre id="filmRevisionData" class="agent-pre"></pre>
                <ul>${p.events.map(event => `<li><small>${new Date(event.at * 1000).toLocaleString('zh-CN')}</small>
                    ${esc(eventDescription(event))}</li>`).join('')}</ul></details>` : ''}
        `;
        root.querySelectorAll('details[id]').forEach(node => { if (open.has(node.id)) node.open = true; });
    }

    const bibleNames = {story: '剧情目标', beats: '情绪节拍', characters: '人物外观', setting: '时空和道具',
        continuity: '空间连续性', camera: '摄影语言', constraints: '创作约束'};
    const opNames = {insert_shot: '新增', remove_shot: '移除', reorder_shot: '移动', revise_shot: '修改',
        select_candidate: '选片', set_trim: '裁切', request_render: '重新生成', mark_human_review: '请求人工审查'};
    function describeOperation(op) {
        return esc([opNames[op.type], op.shot_id, op.after_shot_id && `放在 ${op.after_shot_id} 之后`,
            op.shot?.purpose, op.shot?.prompt, op.clip_id, op.out && `${op.in}–${op.out} 秒`].filter(Boolean).join(' · '));
    }

    function eventDescription(event) {
        const names = {created: '创建项目', plan: '保存分镜计划', queued: '生成入队', model_request: '请求模型',
            attention: '需要处理', approval: '提交审批', clip_review: '单镜审查', continuity: '连续性复盘',
            assembled: '保存合成产物', final_review: '成片审查'};
        const frames = event.frames?.length ? `发送 ${event.frames.length} 张抽帧（${event.frames.map(f =>
            `${f.shot_id || '成片'} ${f.time_sec}秒`).join('、')}）` : '';
        return [names[event.kind] || opNames[event.kind] || {
            pause:'暂停', resume:'继续', stop:'停止', approve:'批准', reject:'拒绝', edit:'人工修改',
            lock:'更新锁定', render:'重新合成', review:'开始复盘'
        }[event.kind] || event.kind, event.model, event.shot_id, frames, event.decision?.analysis || event.reason]
            .filter(Boolean).join(' · ');
    }

    function shotCard(shot, p) {
        const o = shot.options;
        const editable = p.status !== 'stopped';
        const candidates = [...shot.jobs].reverse().map(jid => {
            const job = p.jobs[jid];
            if (!job) return '';
            const review = shot.reviews[jid];
            return `<li>${esc(job.stage || job.status)} ${shot.selected === jid ? '· 已选' : ''}
                ${job.status === 'completed' ? link('/media/job/' + jid + '/video', '预览') : ''}
                ${editable && job.status === 'completed' && shot.selected !== jid ?
                    `<button type="button" class="subtle" data-film-action="candidate" data-clip="${jid}">选用</button>` : ''}
                ${review ? `<small>${esc(reviewDescription(review))}</small>` : ''}</li>`;
        }).join('');
        return `<details id="filmShot${shot.id}" data-shot="${shot.id}">
            <summary>${shot.id} · ${esc(o.title || shot.purpose)} · ${o.seconds} 秒 ${shot.locked ? '· 已锁定' : ''}</summary>
            <p class="agent-muted">${esc([shot.purpose, shot.camera, shot.relation].filter(Boolean).join(' · '))}</p>
            ${editable ? `<label>提示词 <textarea data-field="prompt" rows="2">${esc(o.prompt)}</textarea></label>
                <div class="agent-controls">${button('prompt','保存提示词')}${button('lock',shot.locked ? '解锁' : '锁定')}
                ${button('up','上移')}${button('down','下移')}${button('remove','移除')}</div>` :
                `<p class="agent-pre">${esc(o.prompt)}</p>`}
            ${candidates ? `<ul>${candidates}</ul>` : ''}
            ${editable && shot.selected ? trimFields(shot) : ''}
            </details>`;
    }

    function reviewDescription(review) {
        const verdicts = {technical_failure:'技术失败', visible_quality_failure:'可见质量问题',
            narrative_mismatch:'叙事不符', usable_with_trim:'可裁切使用', pass:'静帧审查通过', uncertain:'待确认'};
        return [verdicts[review.verdict], review.reason, review.trim && `建议裁切 ${review.trim.join('–')} 秒`]
            .filter(Boolean).join(' · ');
    }
    function trimFields(shot) {
        const duration = shot.reviews[shot.selected]?.duration || shot.options.seconds;
        const [start, end] = shot.trim || [0, Math.min(duration, shot.options.seconds)];
        return `<div class="agent-controls film-trim">
            <label>入点（秒） <input type="number" min="0" step="0.001" data-field="in" value="${start}"></label>
            <label>出点（秒） <input type="number" min="0" step="0.001" data-field="out" value="${end}"></label>
            ${button('trim','保存裁切')}
            </div>`;
    }

    function renderActions(p) {
        const ready = p.order.length && p.order.every(sid => p.shots[sid].selected);
        const idle = ['needs_attention','completed','paused','awaiting_approval','blocked'].includes(p.status);
        return [
            ready && idle && button('preview','合成预览'),
            ready && idle && button('final','重新出片'),
            p.revision > 0 && ['needs_attention','completed'].includes(p.status) && button('review','继续制作'),
            Object.keys(p.products).length > 0 && button('folder','打开产物文件夹')
        ];
    }

    function renderProducts(p) {
        return ['final', 'preview'].map(kind => {
            const output = p.products[kind];
            if (!output) return '';
            const prefix = '/media/films/' + p.id + '/' + kind + '/';
            const verdict = {pending:'待审查', pass:'静帧通过', issue:'发现问题', uncertain:'待人工确认'};
            return `<details id="filmProduct${kind}" ${kind === 'final' ? 'open' : ''}>
                <summary>${kind === 'final' ? '正式成片' : '预览'} · 分镜 v${output.revision}${output.revision !== p.revision ? '（历史）' : ''}</summary>
                <p class="agent-muted">技术检查 ${output.technical.status === 'passed' ? '通过' : '失败'} ·
                    内容审查 ${verdict[output.content.verdict]} · ${esc(output.content.reason)}</p>
                <video controls preload="none" src="${prefix}${kind}.mp4?v=${output.id}"></video>
                <div class="agent-controls">${link(prefix + kind + '.mp4?download=1','下载 MP4')}
                    ${link(prefix + 'timeline.json?download=1','时间线')}
                    ${link(prefix + 'film-report.md?download=1','审查报告')}
                    ${link(baseUrl() + '/package','项目包')}
                    ${link(baseUrl() + '/package?media=1','项目包（含剪辑素材）')}</div>
                </details>`;
        }).join('');
    }

    async function perform(control, operation) {
        if (busy) return;
        busy = true;
        control.disabled = true;
        try {
            await operation();
            await refresh(true);
        } catch (error) { toast(error.message, true); }
        finally { control.disabled = false; busy = false; }
    }

    $('filmStart').onclick = event => perform(event.currentTarget, async () => {
        let token;
        const file = $('filmFile').files[0];
        if (file) {
            const response = await fetch('/api/upload?name=' + encodeURIComponent(file.name), {method:'POST', body:file});
            const upload = await response.json();
            if (!response.ok) throw Error(upload.error);
            token = upload.token;
        }
        const budgets = Object.fromEntries([...$('filmSetup').querySelectorAll('[data-budget]')].map(
            input => [input.dataset.budget, input.value]));
        const created = await api('/api/films/start', {
            brief: $('filmBrief').value, target: $('filmTarget').value, token, budgets,
            base: loaded ? collect() : {}, vision: $('filmVision').checked, auto: $('filmAuto').checked
        });
        picker.add(new Option('正在规划', created.id));
        picker.value = created.id;
    });

    picker.onchange = () => refresh(true).catch(error => toast(error.message, true));
    root.onchange = async event => {
        if (event.target.id !== 'filmRevision') return;
        const output = $('filmRevisionData');
        try {
            const version = event.target.value;
            output.textContent = version ? JSON.stringify(await api(baseUrl() + '?revision=' + version), null, 2) : '';
        } catch (error) { toast(error.message, true); }
    };
    root.onclick = event => {
        const control = event.target.closest('[data-film-action]');
        if (!control || !project || picker.value !== project.id) return;
        perform(control, async () => {
            const action = control.dataset.filmAction;
            if (['preview', 'final'].includes(action)) return api(baseUrl() + '/render', {kind:action});
            if (action === 'cancel') return api(baseUrl() + '/stop', {cancel_jobs:true});
            const card = control.closest('[data-shot]');
            const sid = card?.dataset.shot;
            if (action === 'lock') return api(baseUrl() + '/lock', {
                revision:project.revision, shot_id:sid, locked:!project.shots[sid].locked
            });
            const order = project.order;
            const index = order.indexOf(sid);
            if ((action === 'up' && index === 0) || (action === 'down' && index === order.length - 1)) return;
            const operations = {
                prompt: () => ({type:'revise_shot', shot_id:sid, shot:{prompt:card.querySelector('[data-field="prompt"]').value}}),
                up: () => ({type:'reorder_shot', shot_id:sid, after_shot_id:order[index-2] || null}),
                down: () => ({type:'reorder_shot', shot_id:sid, after_shot_id:order[index+1]}),
                remove: () => ({type:'remove_shot', shot_id:sid}),
                candidate: () => ({type:'select_candidate', shot_id:sid, clip_id:control.dataset.clip}),
                trim: () => ({type:'set_trim', shot_id:sid, in:card.querySelector('[data-field=in]').value,
                    out:card.querySelector('[data-field=out]').value})
            };
            if (operations[action]) return api(baseUrl() + '/edit', {
                revision:project.revision, decision:{decision_id:crypto.randomUUID(), based_on_plan_revision:project.revision,
                    analysis:'人工调整', evidence:[], operations:[operations[action]()]}
            });
            await api(baseUrl() + '/' + action, {});
        });
    };
    document.querySelector('[data-page="director"]').addEventListener('click', () =>
        refresh(true).catch(error => toast(error.message, true)));
    setInterval(() => { if (page === 'director' && !busy) refresh().catch(() => {}); }, 3000);
})();
