"""Candidate review and evidence-backed continuity decisions."""
from copy import deepcopy
from pathlib import Path
import time

import film_media
from studio_agent import image_part


VERDICTS = {'technical_failure', 'visible_quality_failure', 'narrative_mismatch',
            'usable_with_trim', 'pass', 'uncertain'}
USABLE = {'usable_with_trim', 'pass'}


class Review:
    def source(self, db, jid):
        row = db.execute('SELECT status,output,error FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row or row['status'] != 'completed' or not row['output']:
            raise ValueError('所选候选缺失或尚未生成。')
        path = Path(row['output']).resolve()
        scope = (self.studio.ROOT / 'jobs' / jid).resolve()
        if not path.is_relative_to(scope) or not path.is_file():
            raise ValueError('候选文件缺失或不在所属任务目录内。')
        return path

    def inspect_clip(self, project, shot, jid, path):
        from film_director import label, number
        try:
            info = film_media.probe(self.studio.FF, path)
            samples = list(film_media.frames(self.studio.FF, path, film_media.sample_times(info['duration']))) if project['vision'] else []
        except ValueError as error:
            return dict(verdict='technical_failure', reason=str(error), frames=[])
        if not project['vision']:
            return dict(verdict='uncertain', reason='已核对媒体信息，待人工审片。', frames=[], **info)
        content, evidence = self.visual_content(
            dict(brief=project['brief'], bible=project['story_bible'], purpose=shot['purpose'],
                 prompt=shot['options']['prompt']), [(shot['id'], jid, meta, image) for meta, image in samples])
        data = self.request(project, project['vision'],
                            '审查同一镜头的静帧。返回 {verdict,reason,frame,trim,prompt}。'
                            'verdict 为 technical_failure/visible_quality_failure/narrative_mismatch/usable_with_trim/pass/uncertain。'
                            'frame 为所见证据的编号，trim 为可用区间 [入点秒,出点秒] 或 null，'
                            'prompt 为内容失败时保持剧情和连续性的完整返修提示词；仅依据静帧，运动和声音不足则表达不确定。',
                            content, evidence)
        verdict = data.get('verdict')
        if verdict not in VERDICTS - {'technical_failure'}:
            raise ValueError('单镜审查结论无效。')
        index = data.get('frame')
        if type(index) is not int or not 1 <= index <= len(evidence):
            raise ValueError('单镜审查的帧编号无效。')
        review = dict(verdict=verdict, reason=label(data.get('reason')), frame=index, frames=evidence, **info)
        if verdict == 'usable_with_trim':
            trim = data.get('trim')
            if not isinstance(trim, list) or len(trim) != 2:
                raise ValueError('建议裁切需要入点和出点。')
            start = number(trim[0], 0, info['duration'])
            review['trim'] = [start, number(trim[1], start + .125, info['duration'])]
        if verdict in {'visible_quality_failure', 'narrative_mismatch'} and data.get('prompt'):
            review['prompt'] = label(data['prompt'], 30000)
        return review

    def visual_content(self, payload, samples):
        content = [{'type': 'text', 'text': self.studio.dumps(payload)}]
        evidence = []
        for sid, jid, frame, image in samples:
            meta = dict(frame, shot_id=sid, clip_id=jid)
            evidence.append(meta)
            content.append({'type': 'text', 'text': self.studio.dumps(meta)})
            content.append(image_part(image))
        return content, evidence

    def generating(self, project):
        with self.studio.db() as db:
            for sid in project['order']:
                shot = project['shots'][sid]
                for jid in shot['active_jobs']:
                    row = db.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()
                    if row and row['status'] in {'queued', 'running'}:
                        return False
        project['status'] = 'clip_review'
        return True

    def review_candidates(self, project):
        for sid in project['order']:
            shot = project['shots'][sid]
            for jid in dict.fromkeys([*shot['active_jobs'], shot['selected']]):
                if not jid or jid in shot['reviews']:
                    continue
                with self.studio.db() as db:
                    row = db.execute('SELECT status,error FROM jobs WHERE id=?', (jid,)).fetchone()
                    try:
                        path = self.source(db, jid)
                    except ValueError as error:
                        shot['reviews'][jid] = dict(verdict='technical_failure', frames=[],
                                                    reason=(row['error'] if row else '') or str(error))
                        return {'shot_id': sid, 'clip_id': jid, 'reason': shot['reviews'][jid]['reason']}
                review = self.inspect_clip(project, shot, jid, path)
                shot['reviews'][jid] = review
                return {'shot_id': sid, 'clip_id': jid, 'model': project['vision'], 'reason': review['reason']}
        return None

    def choose_candidates(self, db, project):
        events = []
        needs_attention = False
        for sid in project['order']:
            shot = project['shots'][sid]
            candidates = [(jid, shot['reviews'][jid]) for jid in shot['active_jobs'] if jid in shot['reviews']]
            if shot['selected'] and shot.get('human_selected'):
                if shot['reviews'][shot['selected']]['verdict'] != 'technical_failure':
                    continue
                needs_attention = True
                continue
            if shot['selected'] and shot['reviews'][shot['selected']]['verdict'] in USABLE:
                continue
            usable = [(jid, r) for jid, r in candidates if r['verdict'] in USABLE]
            if not project['vision']:
                usable = [(jid, r) for jid, r in candidates if r['verdict'] != 'technical_failure']
            if usable:
                jid, review = min(usable, key=lambda item: item[1]['verdict'] == 'usable_with_trim')
                shot.update(selected=jid, trim=review.get('trim'), human_selected=False, human_trim=False)
                events.append(dict(shot_id=sid, clip_id=jid, reason='选用可剪辑的候选'))
                continue
            # An interrupted worker may still own a live Comfy prompt. Never resubmit it here.
            failures = [db.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()
                        for jid, _ in candidates]
            remaining = project['budgets']['generations'] - project['used']['generations']
            reserved = sum(item['options']['candidates'] for item in project['shots'].values() if item['pending_render'])
            if project['auto'] and candidates and not shot.get('technical_retry') and not shot['locked'] and (
                    all(r['verdict'] == 'technical_failure' for _, r in candidates)
                    and all(row and row['status'] in {'failed', 'completed'} for row in failures)
                    and reserved + shot['options']['candidates'] <= remaining):
                shot.update(technical_retry=True, pending_render=True, selected='')
                events.append(dict(shot_id=sid, reason='技术失败，原参数重试一次'))
            else:
                shot['selected'] = ''
                needs_attention = True
        if events:
            self.snapshot(db, project)
            for event in events:
                self.event(db, project, 'candidate_decision', **event)
        if any(project['shots'][sid]['pending_render'] for sid in project['order']):
            project['status'] = 'dispatching'
        elif project['vision'] and (not needs_attention or any(
                r.get('frames') for sid in project['order'] for r in project['shots'][sid]['reviews'].values())):
            project.update(status='continuity_review', transition_reports=[], transition_cursor=0, final_context=None)
        elif needs_attention:
            project.update(status='needs_attention', error='有镜头缺少可用候选，请选片或修改镜头。')
        else:
            self.ready_to_assemble(project)

    def boundary_review(self, project):
        from film_director import label
        cursor = project['transition_cursor']
        left, right = project['order'][cursor:cursor + 2]
        samples = []
        for sid, boundary in ((left, '尾'), (right, '首')):
            shot = project['shots'][sid]
            jid = shot['selected']
            if not jid:
                continue
            with self.studio.db() as db:
                path = self.source(db, jid)
            duration = self.clip_duration(shot, jid)
            start, end = shot['trim'] or (0, duration)
            timestamp = max(start, end - .125) if boundary == '尾' else start
            for frame, image in film_media.frames(self.studio.FF, path, [timestamp]):
                samples.append((sid, jid, dict(frame, boundary=boundary, index=len(samples) + 1), image))
        if len(samples) != 2:
            return dict(shots=[left, right], verdict='uncertain', reason='缺少选用候选，无法比较边界。', frames=[])
        context = dict(bible=project['story_bible'], shots=[dict(id=sid, purpose=project['shots'][sid]['purpose'],
                       relation=project['shots'][sid]['relation']) for sid in (left, right)])
        content, evidence = self.visual_content(context, samples)
        data = self.request(project, project['vision'],
                            '审查前镜尾和后镜首。比对人物、道具、时空、运动方向与轴线；判断缺少建立、反应、动作、插入或过渡镜头。'
                            '返回 {verdict:"pass|issue|uncertain",reason:"两帧可见的具体依据"}。'
                            '静帧无法确认运动时使用 uncertain。', content, evidence,
                            round_start=not project.get('round_open'))
        project['round_open'] = True
        if data.get('verdict') not in {'pass', 'issue', 'uncertain'}:
            raise ValueError('跨镜头审查结论无效。')
        return dict(shots=[left, right], verdict=data['verdict'], reason=label(data.get('reason')), frames=evidence)

    def structure_review(self, project):
        from film_director import label
        shots = []
        for sid in project['order']:
            shot = project['shots'][sid]
            summaries = {jid: {k: r[k] for k in ('verdict', 'reason', 'frames', 'prompt') if k in r}
                         for jid, r in shot['reviews'].items() if jid in shot['active_jobs'] or jid == shot['selected']}
            shots.append(dict(id=sid, purpose=shot['purpose'], prompt=shot['options']['prompt'],
                              seconds=shot['options']['seconds'], trim=shot['trim'], locked=shot['locked'],
                              selected=shot['selected'], reviews=summaries))
        payload = dict(brief=project['brief'], target=project['target'], bible=project['story_bible'],
                       shots=shots, transitions=project['transition_reports'], final=project.get('final_context'),
                       remaining={k: max(0, v - project['used'][k]) for k, v in project['budgets'].items()})
        instruction = (
            '复盘全片结构与情绪节奏，核对因果、叙事空白和目标。优先用已有候选和剪辑修正，明确缺镜才插镜。'
            '返回 {verdict:"pass|change|uncertain",reason:"具体原因",operations:[],evidence:[]}。'
            'change 必须给出非空 operations 及 evidence；其他结论 operations 为空。'
            '证据每项为 {shot_id,clip_id,time_sec}，必须精确引用所列帧。'
            '操作白名单：insert_shot{shot_id,after_shot_id,shot:{purpose,prompt,seconds,candidates}}，'
            'remove_shot{shot_id}，reorder_shot{shot_id,after_shot_id}，revise_shot{shot_id,shot:{prompt}}，'
            'select_candidate{shot_id,clip_id}，set_trim{shot_id,in,out}，mark_human_review{}。'
            '每个操作加 type。镜头 ID 不可复用；after_shot_id 为 null 表示片头。锁定镜头不变。'
            '只有审过的可用素材可以自动选用。内容失败更改提示词，证据不足需人工确认。')
        data = self.request(project, project['model'], instruction, self.studio.dumps(payload),
                            round_start=not project.get('round_open'))
        if data.get('verdict') not in {'pass', 'change', 'uncertain'}:
            raise ValueError('导演复盘结论无效。')
        label(data.get('reason'))
        if data['verdict'] != 'change' and data.get('operations'):
            raise ValueError('审查结论与操作不一致。')
        return data

    def continuity(self, project):
        from film_director import Attention
        status = project['status']
        if project['transition_cursor'] < len(project['order']) - 1:
            report = self.boundary_review(project)
            project['transition_reports'].append(report)
            for frame in report['frames']:
                shot = project['shots'][frame['shot_id']]
                review = shot['reviews'].get(frame['clip_id'])
                if review and frame not in review['frames']:
                    review['frames'].append(frame)
            project['transition_cursor'] += 1
            with self.studio.db() as db:
                db.execute('BEGIN IMMEDIATE')
                self.guard(project, self.load(db, project['id']))
                self.event(db, project, 'continuity', model=project['vision'], **report)
                self.save(db, project)
            return
        data = self.structure_review(project)
        with self.studio.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.guard_epoch(project, self.load(db, project['id']), status)
            project['round_open'] = False
            verdict = data['verdict']
            self.event(db, project, 'structure', reason=data['reason'], model=project['model'], verdict=verdict)
            if verdict == 'change':
                decision = dict(decision_id=f"director-{project['used']['rounds']}-{project['revision']}",
                                based_on_plan_revision=project['revision'], analysis=data['reason'],
                                operations=data.get('operations'), evidence=data.get('evidence'))
                trial = deepcopy(project)
                try:
                    self.apply(db, trial, decision)
                except (Attention, ValueError, TypeError, KeyError) as error:
                    project.update(status='needs_attention', error=f'{data["reason"]}。提议暂停：{error}')
                    self.event(db, project, 'rejected_decision', decision=decision, reason=str(error))
                else:
                    if project['auto']:
                        project.update(trial)
                        self.snapshot(db, project)
                        if project['status'] != 'needs_attention':
                            self.advance(project)
                        self.event(db, project, 'directing', decision=decision, model=project['model'])
                    else:
                        project.update(status='awaiting_approval', pending=decision)
                        self.event(db, project, 'approval', decision=decision, model=project['model'])
            else:
                unresolved = (verdict == 'uncertain' or any(r['verdict'] != 'pass' for r in project['transition_reports']))
                project['structure'] = dict(verdict=verdict, reason=data['reason'], unresolved=unresolved)
                if project.get('final_context'):
                    project.update(status='needs_attention', error='成片审查问题仍待人工确认。')
                elif all(project['shots'][sid]['selected'] for sid in project['order']):
                    self.ready_to_assemble(project)
                else:
                    project.update(status='needs_attention', error='部分镜头缺少可用候选。' + data['reason'])
            self.save(db, project)

    def ready_to_assemble(self, project):
        project.update(status='assembling', output_kind='final')

    def production_tick(self, project):
        status = project['status']
        if status == 'continuity_review':
            self.continuity(project)
            return
        if status not in {'generating', 'clip_review'}:
            return self.output_tick(project)
        details = self.review_candidates(project) if status == 'clip_review' else None
        if status == 'generating' and not self.generating(project):
            project['next_run_at'] = time.time() + 2
        with self.studio.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.guard_epoch(project, self.load(db, project['id']), status)
            if status == 'clip_review':
                if details:
                    self.event(db, project, 'clip_review', **details)
                else:
                    self.choose_candidates(db, project)
            self.save(db, project)

    def output_tick(self, project):
        from film_director import Attention
        raise Attention('已选片，合成阶段待接入。')

