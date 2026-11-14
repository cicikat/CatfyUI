"""Offline Director regression. Uses the real queue schema, HTTP API and FFmpeg."""
import contextlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import film_media
import server
from film_director import Attention, BIBLE, Stale


class Model(server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        system, user = [message['content'] for message in request['messages']]
        if '生成 story_bible' in system:
            result = dict(story_bible={key: key for key in BIBLE}, shots=[
                dict(title=f'镜头 {i}', purpose=f'情节 {i}', prompt=f'画面 {i}', seconds=2)
                for i in range(1, 6)])
        elif '审查同一镜头' in system:
            result = dict(verdict=self.server.clip_verdict, reason='主体清晰', frame=self.server.frame)
        elif '审查前镜尾' in system:
            adjacent = json.loads(user[0]['text'])['shots']
            issue = [s['id'] for s in adjacent] == ['S002', 'S003']
            result = dict(verdict='issue' if issue else 'pass',
                          reason='人物突然坐下，缺动作过渡' if issue else '边界连续')
        elif '复盘全片' in system:
            data = json.loads(user)
            order = [s['id'] for s in data['shots']]
            if any(not item['selected'] for item in data['shots']):
                result = dict(verdict='uncertain', reason='缺少可用候选', operations=[], evidence=[])
            elif 'S002B' not in order:
                frame = next(r['frames'][0] for r in data['transitions'] if r['verdict'] == 'issue')
                evidence = {k: frame[k] for k in ('shot_id', 'clip_id', 'time_sec')}
                result = dict(verdict='change', reason='需要坐下的过渡镜头', evidence=[evidence],
                              operations=[dict(type='insert_shot', shot_id='S002B', after_shot_id='S002',
                                               shot=dict(purpose='动作过渡', prompt='中景拍摄人物坐下', seconds=2))])
            else:
                result = dict(verdict='pass', reason='动作桥接完成', operations=[], evidence=[])
        else:
            result = dict(verdict='pass', reason='关键情节均可见', frame=1)
        payload = json.dumps(dict(choices=[dict(message=dict(content=json.dumps(result)))])).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextlib.contextmanager
def listen(handler):
    with server.ThreadingHTTPServer(('127.0.0.1', 0), handler) as http:
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield http
        finally:
            http.shutdown()
            thread.join()


class DirectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(os.environ.get('CATFYUI_FFMPEG') or shutil.which('ffmpeg') or '')
        probe = cls.ffmpeg.with_name('ffprobe' + cls.ffmpeg.suffix) if cls.ffmpeg.name else Path('.')
        if not cls.ffmpeg.is_file() or not probe.is_file():
            raise unittest.SkipTest('Set CATFYUI_FFMPEG to FFmpeg with adjacent ffprobe')
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture_dir.cleanup)
        cls.fixture = Path(cls.fixture_dir.name) / 'fixture.mp4'
        cls.silent = Path(cls.fixture_dir.name) / 'silent.mp4'
        film_media.ff(cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                     '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2', '-c:v', 'libx264', cls.fixture)
        film_media.ff(cls.ffmpeg, '-v', 'error', '-i', cls.fixture, '-an', '-c:v', 'copy', cls.silent)

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        for directory in ('assets', 'data', 'jobs', 'workflows'):
            (self.folder / directory).mkdir()
        for filename in ('index.html', 'director.js'):
            shutil.copyfile(Path(__file__).with_name(filename), self.folder / filename)
        models = [dict(id=key, ready=True, detail='fake') for key in ('h3-light', 'h3-high', 'h3-max', 'wan22')]
        studio = patch.multiple(server, ROOT=self.folder, DB=self.folder / 'data/test.sqlite',
                                FF=self.ffmpeg, workflows=lambda: models)
        studio.start()
        self.addCleanup(studio.stop)
        server.initialize()
        server.agent().initialize()
        self.d = server.director()
        self.d.initialize()
        model = self.enterContext(listen(Model))
        model.clip_verdict = 'pass'
        model.frame = 1
        self.model = model
        server.setsetting('agent_provider', dict(url=f'http://127.0.0.1:{model.server_port}',
                                              model='fake-director', vision='fake-vision'))

    def queued(self):
        with server.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created,id')]

    def fake_engine(self):
        for job in self.queued():
            if job['status'] != 'queued':
                continue
            folder = self.folder / 'jobs' / job['id']
            folder.mkdir(exist_ok=True)
            output = folder / 'video.mp4'
            fixture = self.fixture if len(self.queued()) <= 5 and job['id'] == self.queued()[0]['id'] else self.silent
            shutil.copyfile(fixture, output)
            server.update(job['id'], status='completed', stage='fake engine', output=str(output))

    def project(self, **options):
        return self.d.start(dict(brief='用镜头讲述人物坐下的经过', target=12, vision=True, auto=True, **options))['id']

    def step(self, pid):
        with server.db() as db:
            project = self.d.load(db, pid)
        self.d.tick(project)

    def pump(self, pid, until):
        for _ in range(120):
            p = self.d.state(pid)
            if p['status'] in until:
                return p
            self.fake_engine()
            self.step(pid)
        self.fail(self.d.state(pid)['status'])

    def decision(self, p, operations, identifier='human-edit'):
        return dict(revision=p['revision'], decision=dict(decision_id=identifier,
                    based_on_plan_revision=p['revision'], analysis='人工测试修改',
                    operations=operations, evidence=[]))

    def test_continuity_action_and_timeline_render(self):
        with listen(server.Handler) as http:
            with patch.multiple(server, PORT=http.server_port, URL=f'http://127.0.0.1:{http.server_port}'):
                base = server.URL
                def api(path, data=None):
                    request = urllib.request.Request(base + path, None if data is None else json.dumps(data).encode())
                    with urllib.request.urlopen(request) as response:
                        return json.load(response)
                pid = api('/api/films/start', dict(brief='人物坐下', target=12, vision=True, auto=True))['id']
                project_url = '/api/films/' + pid
                p = self.pump(pid, {'completed', 'needs_attention'})
                self.assertEqual(p['status'], 'completed', p['error'])
                self.assertEqual(p['order'], ['S001', 'S002', 'S002B', 'S003', 'S004', 'S005'])
                self.assertEqual(len(self.queued()), 6)
                self.assertNotIn('S002B', api(project_url + '?revision=1')['order'])
                self.assertTrue(any(e['kind'] == 'directing' and e['decision']['evidence'] for e in api(project_url)['events']))
                video = self.d.artifact(pid, 'final', 'final.mp4')
                original = video
                timeline = self.d.artifact(pid, 'final', 'timeline.json')
                self.assertEqual(server.read(timeline)['schema_version'], 1)
                technical = p['products']['final']['technical']
                self.assertAlmostEqual(technical['duration'], 12, delta=.25)
                self.assertTrue(technical['audio'])
                self.assertEqual((technical['width'], technical['height'], technical['fps']), (1024, 576, '24/1'))
                with urllib.request.urlopen(urllib.request.Request(
                        base + f'/media/films/{pid}/final/final.mp4', headers={'Range': 'bytes=0-63'})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(len(response.read()), 64)
                api(project_url + '/edit', self.decision(p, [dict(type='set_trim', shot_id='S001',
                                                                     **{'in': .25, 'out': 1.75})]))
                api(project_url + '/render', dict(kind='final'))
                p = self.pump(pid, {'completed', 'needs_attention'})
                self.assertEqual(len(self.queued()), 6)
                self.assertAlmostEqual(p['products']['final']['technical']['duration'], 11.5, delta=.25)
                self.assertTrue(original.is_file())
                self.assertNotEqual(original, self.d.artifact(pid, 'final', 'final.mp4'))
                bundle = self.folder / 'film.zip'
                self.d.bundle(pid, bundle, include_media=True)
                with zipfile.ZipFile(bundle) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertIn('final/final.mp4', archive.namelist())
                    self.assertEqual(sum(name.startswith('jobs/') for name in archive.namelist()), 6)
                evidence_dir = os.environ.get('CATFYUI_TEST_EVIDENCE')
                if evidence_dir:
                    dest = Path(evidence_dir)
                    dest.mkdir(parents=True, exist_ok=True)
                    for name in ('final.mp4', 'timeline.json', 'film-report.json', 'film-report.md'):
                        shutil.copy2(self.d.artifact(pid, 'final', name), dest / name)
                    shutil.copy2(original, dest / 'original-12s.mp4')
                    shutil.copy2(bundle, dest / 'film.zip')
                    server.write(dest / 'events.json', p['events'])
                    print(f'Offline test evidence: {dest}')

    def test_plan_revision_and_manual_override(self):
        pid = self.d.start(dict(brief='片段', auto=False))['id']
        p = self.pump(pid, {'awaiting_approval'})
        self.assertEqual(self.queued(), [])
        with self.assertRaises(ValueError):
            self.d.action(pid, 'edit', self.decision(p, [dict(type='remove_shot', shot_id='S999')]))
        self.d.action(pid, 'lock', dict(revision=p['revision'], shot_id='S002', locked=True))
        p = self.d.state(pid)
        data = self.decision(p, [dict(type='remove_shot', shot_id='S004'),
                                 dict(type='reorder_shot', shot_id='S005', after_shot_id='S001')])
        self.d.action(pid, 'edit', data)
        self.d.action(pid, 'edit', data)
        self.assertEqual(self.d.state(pid)['order'], ['S001', 'S005', 'S002', 'S003'])
        self.assertEqual(len(self.d.state(pid, 1)['order']), 5)
        self.assertTrue(self.d.state(pid)['shots']['S002']['locked'])
        self.d.action(pid, 'approve', {})
        self.d.action(pid, 'pause', {})
        self.assertEqual(self.queued(), [])
        self.d.action(pid, 'resume', {})
        self.step(pid)
        self.assertEqual(len(self.queued()), 4)

    def test_resume_idempotency(self):
        pid = self.project()
        self.step(pid)
        real = server.enqueue
        def interrupt(options, connection):
            real(options, connection)
            raise RuntimeError('simulated interruption before commit')
        with patch.object(server, 'enqueue', interrupt), self.assertRaises(RuntimeError):
            self.step(pid)
        self.assertEqual(self.queued(), [])
        with server.db() as db:
            stale = self.d.load(db, pid)
        self.d = server.director()
        self.step(pid)
        with self.assertRaises(Stale):
            self.d.tick(stale)
        self.assertEqual(len(self.queued()), 5)

    def test_recover_original_wan_receipt(self):
        options = server.validate(dict(prompt='Wan', workflow='wan22', candidates=1))
        options['director_project'] = 'test-project'
        jid, missing = server.enqueue([options, options])
        for item in (jid, missing):
            server.update(item, status='running')
        folder = self.folder / 'jobs' / jid
        folder.mkdir()
        server.write(folder / 'comfy-prompt.json', {'prompt_id': 'original-prompt'})
        server.initialize()
        self.d.recover_jobs()
        self.assertEqual(server.job(jid)['status'], 'queued')
        self.assertEqual(server.job(missing)['status'], 'interrupted')
        self.assertEqual(server.read(folder / 'comfy-prompt.json')['prompt_id'], 'original-prompt')
        self.assertEqual(len(self.queued()), 2)

    def test_budget_limits(self):
        pid = self.project(budgets=dict(generations=5))
        p = self.pump(pid, {'needs_attention'})
        self.assertIn('预算', p['error'])
        self.assertEqual(len(self.queued()), 5)
        other = self.project(budgets=dict(calls=1, rounds=0))
        self.step(other)
        self.step(other)
        self.fake_engine()
        self.step(other)
        with self.assertRaisesRegex(Attention, '模型调用'):
            self.step(other)
        self.assertEqual(self.d.state(other)['used']['calls'], 1)

    def test_clip_review_mock(self):
        pid = self.project()
        self.step(pid)
        self.step(pid)
        self.fake_engine()
        self.step(pid)
        self.model.clip_verdict = 'uncertain'
        self.step(pid)
        p = self.d.state(pid)
        reviews = [r for s in p['shots'].values() for r in s['reviews'].values()]
        self.assertEqual(reviews[0]['verdict'], 'uncertain')
        self.model.clip_verdict = 'narrative_mismatch'
        self.step(pid)
        self.model.frame = 999
        with self.assertRaisesRegex(ValueError, '帧编号'):
            self.step(pid)
        self.model.frame = 1
        self.model.clip_verdict = 'pass'
        p = self.pump(pid, {'needs_attention'})
        self.assertFalse(p['products'])
        self.assertTrue(any(r['verdict'] == 'narrative_mismatch'
                            for shot in p['shots'].values() for r in shot['reviews'].values()))

    def test_final_state_without_vision(self):
        pid = self.d.start(dict(brief='片段', auto=True, target=10))['id']
        p = self.pump(pid, {'completed', 'needs_attention'})
        self.assertEqual(p['status'], 'needs_attention')
        self.assertEqual(p['products']['final']['technical']['status'], 'passed')
        self.assertEqual(p['products']['final']['content']['verdict'], 'uncertain')

    def test_regression(self):
        options = server.validate(dict(prompt='手工生成'))
        self.assertEqual(len(server.enqueue([options])), 2)
        self.assertEqual(server.validate(dict(prompt='手动 Wan', workflow='wan22'))['workflow'], 'wan22')
        csv_file = self.folder / 'assets' / 'shots.csv'
        csv_file.write_text('title,prompt,seconds\nA,Test,2\n', encoding='utf-8')
        self.assertEqual(server.batch_parse(csv_file, server.DEFAULT)[0]['options']['title'], 'A')
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(('title', 'prompt'))
        workbook.active.append(('B', 'Test'))
        xlsx = csv_file.with_suffix('.xlsx')
        workbook.save(xlsx)
        self.assertEqual(server.batch_parse(xlsx, server.DEFAULT)[0]['options']['title'], 'B')
        self.assertEqual(server.agent().state()['runs'], [])


if __name__ == '__main__':
    unittest.main()
