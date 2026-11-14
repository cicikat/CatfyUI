"""Timeline rendering, technical verification and final content review."""
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

import film_media


ARTIFACTS = {'preview.mp4', 'final.mp4', 'timeline.json', 'film-report.json', 'film-report.md'}


class Output:
    def extra_action(self, db, project, action, data):
        if action == 'review' and project['status'] in {'needs_attention', 'completed'}:
            project.update(status='generating', error='')
            return
        if action != 'render' or project['status'] not in {
                'needs_attention', 'completed', 'paused', 'awaiting_approval', 'blocked'}:
            raise ValueError('请先暂停导演，或等当前阶段结束。')
        kind = data.get('kind')
        if kind not in ('preview', 'final'):
            raise ValueError('请选择预览或正式成片。')
        if not project['order'] or any(not project['shots'][sid]['selected'] for sid in project['order']):
            raise ValueError('请先为每个镜头选用候选。')
        if project['used']['assemblies'] >= project['budgets']['assemblies']:
            raise ValueError('已达到合成预算。')
        project.update(status='assembling', output_kind=kind, error='', pending=None, assembly_id=None)

    def ready_to_assemble(self, project):
        project.update(status='assembling', output_kind='final', assembly_id=None)

    def timeline(self, project):
        clips = []
        with self.studio.db() as db:
            for sid in project['order']:
                shot = project['shots'][sid]
                jid = shot['selected']
                source = self.source(db, jid)
                meta = film_media.probe(self.studio.FF, source)
                start, end = shot['trim'] or (0, min(meta['duration'], shot['options']['seconds']))
                if not 0 <= start < end <= meta['duration'] + .001 or end - start < .125:
                    raise ValueError(f'{sid} 的裁切超出素材范围。')
                clips.append(dict(shot_id=sid, job_id=jid, source=source.relative_to(self.studio.ROOT).as_posix(),
                                  audio=meta['audio'], in_sec=start, out_sec=end,
                                  duration=round(end - start, 6), target_seconds=shot['options']['seconds'],
                                  crop='fit_with_padding', transition='cut'))
        if not clips:
            raise ValueError('时间线没有镜头。')
        return dict(schema_version=1, project_id=project['id'], revision=project['revision'],
                    assembly=project['assembly_id'], fps=project['fps'], resolution=project['resolution'],
                    target_seconds=project['target'], tracks=[dict(type='video', clips=clips)], overlays=[])

    def assemble(self, project, folder, timeline, filename):
        clips = timeline['tracks'][0]['clips']
        encoder = self.studio.FF
        fps = timeline['fps']
        width, height = timeline['resolution'].split('x')
        with tempfile.TemporaryDirectory(prefix='segments-', dir=folder) as scratch:
            scratch = Path(scratch)
            entries = []
            with_audio = any(clip['audio'] for clip in clips)
            for index, clip in enumerate(clips):
                name = f'segment-{index:04}.mp4'
                entries.append(f"file '{name}'")
                duration = clip['duration']
                source = self.studio.ROOT / clip['source']
                args = ['-v', 'error', '-xerror', '-y', '-ss', clip['in_sec'], '-i', source]
                if with_audio and not clip['audio']:
                    args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
                args += ['-t', duration, '-map', '0:v:0', '-vf',
                         f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                         f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p',
                         '-c:v', 'libx264', '-preset', 'fast', '-crf', '18']
                if with_audio:
                    args += ['-map', '0:a:0' if clip['audio'] else '1:a:0',
                             '-af', 'apad', '-c:a', 'aac', '-ar', '48000', '-ac', '2']
                else:
                    args += ['-an']
                film_media.ff(encoder, *args, scratch / name, timeout=600)
            listing = scratch / 'segments.txt'
            listing.write_text('\n'.join(entries) + '\n', encoding='ascii')
            film_media.ff(encoder, '-v', 'error', '-y', '-f', 'concat', '-safe', 1, '-i', listing,
                          '-c', 'copy', '-movflags', '+faststart', folder / filename, timeout=600)

    def verify(self, project, path, timeline):
        encoder = self.studio.FF
        clips = timeline['tracks'][0]['clips']
        media = film_media.probe(encoder, path)
        film_media.ff(encoder, '-v', 'error', '-xerror', '-i', path, '-map', '0:v:0',
                      '-map', '0:a?', '-f', 'null', '-', timeout=600)
        black_intervals = film_media.black_intervals(encoder, path)
        planned = sum(clip['duration'] for clip in clips)
        discrepancy = abs(media['duration'] - planned)
        warnings = []
        technical_issues = []
        if discrepancy > max(.25, len(clips) / timeline['fps'] * 1.5):
            technical_issues.append('编码时长与时间线不一致。')
        if media['audio'] and abs(media['audio_duration'] - media['duration']) > max(.25, len(clips) * .03):
            technical_issues.append('音视频轨时长不一致。')
        if black_intervals:
            warnings.append('检测到持续黑场，请确认是否为有意剪辑。')
        if abs(media['duration'] - project['target']) > max(1, project['target'] * .15):
            warnings.append('实际片长与目标差异较大。')
        return dict(status='failed' if technical_issues else 'passed', decode='passed', timeline_resolved=True,
                    **media, planned_seconds=round(planned, 6), target_seconds=project['target'],
                    delta_from_target=round(media['duration'] - project['target'], 6),
                    black_intervals=black_intervals, issues=technical_issues, warnings=warnings,
                    sha256=film_media.digest(path))

    def write_report(self, project, folder, technical, content):
        payload = dict(project_id=project['id'], revision=project['revision'], technical=technical,
                       content=content, budgets=project['budgets'], used=project['used'],
                       structure=project.get('structure'), transitions=project.get('transition_reports', []),
                       clips={sid: project['shots'][sid]['reviews'] for sid in project['order']})
        self.studio.write(folder / 'film-report.json', payload)
        technical_name = {'passed': '通过', 'failed': '失败'}.get(technical['status'], '待检查')
        content_name = {'pass': '静帧通过', 'issue': '发现问题', 'uncertain': '待人工确认',
                        'pending': '待审查'}.get(content['verdict'], '待审查')
        report = ['# 成片审查', '', f"- 技术检查 {technical_name}",
                  f"- 内容审查 {content_name}。{content['reason']}",
                  f"- 片长 {technical.get('duration', 0):.2f} 秒，目标 {project['target']:.2f} 秒",
                  f"- SHA256 {technical.get('sha256', '—')}"]
        report.extend(f'- {warning}' for warning in technical.get('issues', []) + technical.get('warnings', []))
        report.extend(['', '视觉审查依据是抽帧。连续运动、声音与完整节奏仍需人工播放核对。'])
        temporary = folder / 'film-report.md.tmp'
        temporary.write_text('\n'.join(report) + '\n', encoding='utf-8')
        os.replace(temporary, folder / 'film-report.md')

    def begin_assembly(self, project):
        if project.get('assembly_id'):
            return
        with self.studio.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.guard(project, self.load(db, project['id']))
            self.charge(project, 'assemblies')
            project['assembly_id'] = uuid.uuid4().hex
            self.event(db, project, 'assembly_start')
            self.save(db, project)

    def output_tick(self, project):
        if project['status'] == 'final_review':
            return self.final_review(project)
        self.begin_assembly(project)
        folder = self.folder(project['id']) / project['assembly_id']
        folder.mkdir(exist_ok=True)
        kind = project['output_kind']
        filename = kind + '.mp4'
        timeline = self.timeline(project)
        self.assemble(project, folder, timeline, filename)
        technical = self.verify(project, folder / filename, timeline)
        self.studio.write(folder / 'timeline.json', timeline)
        content = dict(verdict='pending', reason='成片等待审查。', frames=[])
        if kind == 'preview':
            content = dict(verdict='uncertain', reason='预览等待人工播放。', frames=[])
        with self.studio.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.guard(project, self.load(db, project['id']))
            self.write_report(project, folder, technical, content)
            project['products'][kind] = dict(id=project['assembly_id'], technical=technical,
                                             content=content, revision=project['revision'])
            project['assembly_id'] = None
            if technical['status'] == 'failed':
                project.update(status='needs_attention', error='成片技术检查失败。')
            elif kind == 'preview':
                project.update(status='needs_attention', error='预览已生成，可调整剪辑或出正式成片。')
            else:
                project.update(status='final_review', error='')
            self.event(db, project, 'assembled', output=kind, technical=technical['status'])
            self.save(db, project)

    def final_review(self, project):
        from film_director import label
        output = project['products']['final']
        folder = self.folder(project['id']) / output['id']
        technical = output['technical']
        if not project['vision']:
            report = dict(verdict='uncertain', reason='已完成技术检查，请人工播放审查成片。', frames=[])
        else:
            timeline = self.studio.read(folder / 'timeline.json')
            clips = timeline['tracks'][0]['clips']
            duration = technical['duration']
            times = film_media.sample_times(duration)
            boundaries = []
            elapsed = 0
            for clip in clips[:-1]:
                elapsed += clip['duration']
                boundaries.append(elapsed)
            if boundaries:
                jumps = min(6, len(boundaries))
                times = sorted(set(times + [round(boundaries[i * len(boundaries) // jumps], 6)
                                            for i in range(jumps)]))
                times = [value for value in times if value <= duration - .125]
            samples = [(None, output['id'], frame, image) for frame, image in
                       film_media.frames(self.studio.FF, folder / 'final.mp4', times)]
            content, evidence = self.visual_content(dict(
                brief=project['brief'], bible=project['story_bible'], structure=project.get('structure'),
                timeline=[{k: clip[k] for k in ('shot_id', 'duration', 'in_sec', 'out_sec')} for clip in clips]), samples)
            data = self.request(project, project['vision'],
                                '按成片时间抽帧复核叙事覆盖、连续性和节奏。'
                                '返回 {verdict:"pass|issue|uncertain",reason:"依据",frame:1}。'
                                'frame 为实际证据编号。运动、声音和完整节奏无法仅由静帧确认。',
                                content, evidence)
            frame = data.get('frame')
            if data.get('verdict') not in {'pass', 'issue', 'uncertain'} or type(frame) is not int or not 1 <= frame <= len(evidence):
                raise ValueError('成片审查结论或证据编号无效。')
            report = dict(verdict=data['verdict'], reason=label(data.get('reason')), frame=frame, frames=evidence)
        with self.studio.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.guard(project, self.load(db, project['id']))
            output['content'] = report
            self.write_report(project, folder, technical, report)
            structure = project.get('structure') or {}
            structure_ok = (structure.get('revision') == project['revision'] and
                            structure.get('verdict') == 'pass' and not structure['unresolved'])
            if (technical['status'] == 'passed' and not technical['warnings']
                    and report['verdict'] == 'pass' and structure_ok):
                project.update(status='completed', error='')
            elif (project['auto'] and project['vision'] and report['verdict'] == 'issue'
                  and project['used']['rounds'] < project['budgets']['rounds']
                  and project['used']['calls'] < project['budgets']['calls']
                  and project['used']['assemblies'] < project['budgets']['assemblies']):
                project.update(status='continuity_review', final_context=report,
                               transition_cursor=len(project['order']) - 1, round_open=False)
            else:
                project.update(status='needs_attention', error=report['reason'] if report['verdict'] != 'pass'
                               else '静帧检查通过，技术提示或连续性问题仍需人工确认。')
            self.event(db, project, 'final_review', model=project['vision'], **report)
            self.save(db, project)

    def artifact(self, pid, kind, filename):
        if filename not in ARTIFACTS or filename.endswith('.mp4') and filename != kind + '.mp4':
            raise ValueError('产物名称无效。')
        if kind not in ('preview', 'final'):
            raise ValueError('产物类型无效。')
        with self.studio.db() as db:
            output = self.load(db, pid)['products'].get(kind)
        if not output:
            raise ValueError('该产物尚未生成。')
        return self.folder(pid) / output['id'] / filename

    def bundle(self, pid, destination, include_media=False):
        with self.studio.db() as db:
            db.execute('BEGIN')
            project = self.load(db, pid)
            revisions = [tuple(row) for row in db.execute(
                'SELECT revision,value FROM film_shots WHERE project_id=? ORDER BY revision', (pid,))]
            events = [row[0] for row in db.execute(
                'SELECT value FROM film_events WHERE project_id=? ORDER BY seq', (pid,))]
        project.pop('imported', None)
        project.pop('url', None)
        project.pop('permitted_assets', None)
        with zipfile.ZipFile(destination, 'w', allowZip64=True) as archive:
            archive.writestr('project.json', self.studio.dumps(project))
            archive.writestr('events.jsonl', '\n'.join(events) + '\n')
            for revision, revision_json in revisions:
                archive.writestr(f'shot-plan.v{revision}.json', revision_json)
            if include_media:
                with self.studio.db() as db:
                    for jid in {shot['selected'] for shot in project['shots'].values()} - {''}:
                        path = self.source(db, jid)
                        archive.write(path, path.relative_to(self.studio.ROOT).as_posix())
            for kind, output in project['products'].items():
                folder = self.folder(pid) / output['id']
                for filename in ARTIFACTS & {path.name for path in folder.iterdir()}:
                    archive.write(folder / filename, f'{kind}/{filename}')
                if include_media:
                    with self.studio.db() as db:
                        for clip in self.studio.read(folder / 'timeline.json')['tracks'][0]['clips']:
                            path = self.source(db, clip['job_id'])
                            if clip['source'] not in archive.namelist():
                                archive.write(path, clip['source'])
