"""Loopback-only, persistent local video studio. No modifications to source workflows."""
import contextlib, csv, ctypes, hashlib, io, json, math, mimetypes, os, random, shutil, sqlite3, subprocess, sys, threading, time, traceback, urllib.parse, urllib.request, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import psutil
import tempfile
import studio_agent
from film_director import Director

ROOT=Path(__file__).resolve().parent
H3 = Path(os.environ.get('CATFYUI_H3_ROOT') or ROOT / 'engines' / 'FreeVideo-H3')
COMFY = Path(os.environ.get('CATFYUI_COMFY_ROOT') or ROOT / 'engines' / 'ComfyUI')
FF = Path(os.environ.get('CATFYUI_FFMPEG') or shutil.which('ffmpeg') or ROOT / 'ffmpeg.exe')
PORT=int(os.environ.get('VIDEO_STUDIO_PORT','8788')); URL=f'http://127.0.0.1:{PORT}'
for name in ['data','assets','jobs','workflows']: (ROOT/name).mkdir(exist_ok=True)
DB=ROOT/'data/studio.sqlite'; mutex=threading.RLock(); wake=threading.Event(); stopped=threading.Event(); active={}; runtime={'phase':'就绪'}
DEFAULT={'workflow':'h3-light','prompt':'','negative':'text, subtitles, watermark, distorted anatomy, extra limbs, sudden objects, impossible physics','seconds':3,'resolution':'1024x576','seed':-1,'candidates':2,'media_mode':'frames','first':'','last':'','references':[],'title':'','steps':20,'cfg':5}

@contextlib.contextmanager
def db():
    c=sqlite3.connect(DB,timeout=20);c.row_factory=sqlite3.Row
    try:
        with c:yield c
    finally:c.close()
def dumps(v): return json.dumps(v,ensure_ascii=False)
def read(p): return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):
    p=Path(p); t=p.with_suffix(p.suffix+'.tmp'); t.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8'); os.replace(t,p)
def initialize():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,path TEXT UNIQUE,name TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,title TEXT,status TEXT,options TEXT,created REAL,updated REAL,progress REAL DEFAULT 0,stage TEXT,error TEXT DEFAULT '',output TEXT DEFAULT '',cancel INTEGER DEFAULT 0);
        ''')
        c.execute("UPDATE jobs SET status='interrupted',stage='服务曾中断，请重试；保留原任务',updated=? WHERE status='running'",(time.time(),))
    templates={'h3':H3/'example_workflows/FreeVideo-All-in-One.json','wan':COMFY/'user/default/workflows/wan22_5b_i2v_template.json'}
    for name,source in templates.items():
        dest=ROOT/'workflows'/f'{name}-source.json'
        if not dest.exists() and source.is_file(): shutil.copy2(source,dest)
def setting(k,default=None):
    with db() as c: row=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
    return json.loads(row[0]) if row else default
def setsetting(k,v):
    with db() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,dumps(v)))
def asset(path):
    p=Path(path).resolve()
    if not p.is_file() or p.suffix.lower() not in ('.png','.jpg','.jpeg','.webp','.bmp'):raise ValueError('参考图不存在或格式不支持：'+str(p))
    aid=hashlib.sha256(str(p).encode()).hexdigest()[:24]
    with db() as c:c.execute('INSERT OR REPLACE INTO assets VALUES(?,?,?)',(aid,str(p),p.name))
    return {'id':aid,'name':p.name,'url':'/media/asset/'+aid,'path':str(p)}
def assetpath(aid, connection=None):
    with (db() if connection is None else contextlib.nullcontext(connection)) as c:
        r=c.execute('SELECT path FROM assets WHERE id=?',(aid,)).fetchone()
    if not r or not Path(r[0]).is_file():raise ValueError('参考图已丢失，请重新选择')
    return r[0]
def workflows():
    h3ready=(H3/'machine.json').is_file() and read(H3/'machine.json').get('ready',False) and (ROOT/'workflows/h3-source.json').is_file() and FF.is_file()
    needed=[COMFY/'models'/d/n for d,n in [('diffusion_models','wan2.2_ti2v_5B_fp16.safetensors'),('text_encoders','umt5_xxl_fp8_e4m3fn_scaled.safetensors'),('vae','wan2.2_vae.safetensors')]]
    missing=[p.name for p in needed if not p.is_file() or p.stat().st_size<1024**2]
    if not (ROOT/'workflows/wan-source.json').is_file():
        missing.append('workflows/wan-source.json')
    if not FF.is_file():
        missing.append('FFmpeg')
    return [dict(id='h3-'+t,name='H3 '+t.title(),ready=h3ready,adapter='H3 原生引擎 · 现有 JSON 适配',detail=desc if h3ready else '需要配置 FreeVideo-H3、工作流及 FFmpeg') for t,desc in [('light','快速预览 · 8 + 3 步'),('high','正式素材 · 16 步'),('max','精细生成 · 20 步')]]+[dict(id='wan22',name='Wan 2.2',ready=not missing,adapter='ComfyUI · 现有 JSON 工作流',detail='缺少模型：'+', '.join(missing) if missing else 'TI2V 5B · 首帧 / 文生视频')]
def default_asset_root():
    return str(ROOT / 'assets')
def validate(raw,require_ready=True,asset_connection=None):
    o={**DEFAULT,**raw}; models={m['id']:m for m in workflows()}
    if o['workflow'] not in models:raise ValueError('未知工作流')
    if require_ready and not models[o['workflow']]['ready']:raise ValueError(models[o['workflow']]['detail'])
    o['prompt']=str(o['prompt']).strip()
    if not o['prompt'] or len(o['prompt'])>30000:raise ValueError('请输入提示词（最多 30000 字符）')
    o['seconds']=float(o['seconds']); o['seed']=int(o['seed']); o['candidates']=int(o['candidates']); o['steps']=int(o['steps']); o['cfg']=float(o['cfg'])
    if not 1.625<=o['seconds']<=15:raise ValueError('时长需为 1.625–15 秒')
    if not 1<=o['candidates']<=8 or not -1<=o['seed']<=2**53-9:raise ValueError('候选数量 1–8；种子需为 -1 或有效正整数')
    if not 1<=o['steps']<=50 or not 0<=o['cfg']<=20:raise ValueError('Wan 步数 1–50，CFG 0–20')
    if o['resolution'] not in ['768x448','1024x576','576x1024','1536x864']:raise ValueError('不支持的分辨率')
    if o['media_mode'] not in ['frames','references']:raise ValueError('参考模式无效')
    if o['media_mode']=='frames':o['references']=[]
    else:o['first']='';o['last']=''
    if len(o['references'])>8:raise ValueError('参考图最多 8 张')
    for aid in [o['first'],o['last']]+o['references']:
        if aid:assetpath(aid, asset_connection)
    if o['workflow']=='wan22' and (o['last'] or o['references']):raise ValueError('当前 Wan JSON 仅支持首帧或文生视频；请选择首尾帧模式并清空尾帧')
    return o
def enqueue(options, connection=None):
    ids=[]; now=time.time()
    with (db() if connection is None else contextlib.nullcontext(connection)) as c:
        for o in options:
            seed=random.randrange(2**32) if o['seed']==-1 else o['seed']
            for i in range(o['candidates']):
                item=dict(o,seed=seed+i,candidates=1); jid=time.strftime('%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:7]
                title=(str(o.get('title','')).strip() or o['prompt'][:28])+f' · {i+1:02}'
                c.execute('INSERT INTO jobs(id,title,status,options,created,updated,stage) VALUES(?,?,?,?,?,?,?)',(jid,title,'queued',dumps(item),now,now,'排队中')); ids.append(jid)
    wake.set();return ids
def update(jid,**values):
    values['updated']=time.time()
    with db() as c:c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),jid))
def job(jid):
    with db() as c:r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
    if not r:raise ValueError('任务不存在')
    return dict(r)
def cancelled(jid):return stopped.is_set() or bool(job(jid)['cancel'])
def kill(proc):
    try:
        parent=psutil.Process(proc.pid)
        for child in reversed(parent.children(recursive=True)):
            with contextlib.suppress(psutil.Error):child.kill()
        parent.kill()
    except psutil.Error:pass
def resource_busy():
    owned=set()
    if active.get('proc'):
        with contextlib.suppress(psutil.Error):
            parent=psutil.Process(active['proc'].pid);owned={parent.pid,*[p.pid for p in parent.children(recursive=True)]}
    for p in psutil.process_iter(['pid','cmdline']):
        try:
            if p.pid in owned:continue
            s=' '.join(p.info['cmdline'] or [])
            if ('freevideo_engine' in s and any(x in s for x in [' generate ','freevideo_engine.worker',' encode '])) or ('main.py' in s and ('ComfyUI' in s or str(COMFY) in s)):
                return '其他生成进程正在使用资源'
        except (psutil.Error,TypeError):pass
    return ''
def wait_resources(jid):
    while True:
        if cancelled(jid):raise InterruptedError('已取消')
        reason=resource_busy()
        class Memory(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(n,ctypes.c_ulonglong) for n in ['total_phys','avail_phys','total_commit','avail_commit','total_virtual','avail_virtual','extended']]
        mem=Memory();mem.length=ctypes.sizeof(mem);ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        if not reason and min(mem.avail_phys,mem.avail_commit)<12*2**30:reason='等待至少 12 GiB 物理 / 提交内存余量'
        if not reason and shutil.disk_usage(ROOT).free<6*2**30:reason='磁盘不足 6 GiB，等待释放空间'
        if not reason:return
        update(jid,stage=reason);time.sleep(3)
def h3_plan(o,folder):
    template=read(ROOT/'workflows/h3-source.json')
    if not any(n['type']=='FreeVideoGenerate' for n in template['nodes']):raise ValueError('H3 JSON 缺少 FreeVideoGenerate 节点')
    tier=o['workflow'].split('-')[1]; w,h=map(int,o['resolution'].split('x'));m=read(H3/'machine.json')
    media={'version':1}
    snapshots={}; evidence=[]
    for aid in [o['first'],o['last']]+o['references']:
        if not aid or aid in snapshots:continue
        source=Path(assetpath(aid));dest=folder/'reference-images'/source.name;dest.parent.mkdir(exist_ok=True)
        dest=dest.with_name(aid[:8]+'-'+dest.name);shutil.copy2(source,dest);snapshots[aid]=str(dest)
        evidence.append({'asset_id':aid,'source':str(source),'snapshot':str(dest),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
    write(folder/'reference-evidence.json',evidence)
    if o['media_mode']=='references':media['references']=[{'kind':'image','path':snapshots[x]} for x in o['references']]
    else:
        for key in ['first','last']:
            if o[key]:media[key]=snapshots[o[key]]
    write(folder/'media.json',media);(folder/'prompt.txt').write_text(o['prompt'],encoding='utf-8')
    for n in template['nodes']:
        if n['type']=='FreeVideoGenerate':
            n['widgets_values']=[o['prompt'],w,h,o['seconds'],o['seed'],'fixed',tier=='light']
    template.setdefault('extra',{})['studio_resolved_plan']='resolved-plan.json'
    write(folder/'source-workflow.json',template)
    write(folder/'resolved-plan.json',dict(adapter='native-h3',source=str(ROOT/'workflows/h3-source.json'),tier=tier,options=o,media=media,steps=8 if tier=='light' else 16 if tier=='high' else 20))
    argv=[m['python'],'-u','-m','freevideo_engine','generate','--cache',m['cache'],'--base',m['base'],'--checkpoint',m['checkpoint'],'--encoder-python',m['python'],'--encoder-root',str(H3/'vendor/h3-text-encoder'),'--model-paths',m['model_paths'],'--encoder',m['encoder'],'--prompt-file',str(folder/'prompt.txt'),'--out',str(folder/'video.mp4'),'--width',str(w),'--height',str(h),'--seconds',str(o['seconds']),'--seed',str(o['seed']),'--two-pass' if tier=='light' else '--no-two-pass','--base-steps',str(8 if tier=='light' else 16 if tier=='high' else 20),'--refine-steps','3','--upscale-factor','2','--ram-reserve-gib','6','--resource-retries','1']
    if any(k in media for k in ['first','last','references']):argv+=['--media',str(folder/'media.json')]
    if tier!='light':argv+=['--step-cache','easycache-av']
    return argv
def execute_h3(jid,o,folder):
    wait_resources(jid); argv=h3_plan(o,folder);write(folder/'command.json',argv);started=time.monotonic()
    env=dict(os.environ,FREEVIDEO_HOME=str(H3),PYTHONPATH=str(H3),PYTHONUTF8='1',PYTHONIOENCODING='utf-8');env['PATH']=str(FF.parent)+os.pathsep+env.get('PATH','')
    with (folder/'generate.log').open('w',encoding='utf-8') as log:
        proc=subprocess.Popen(argv,cwd=H3,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        active.update(jid=jid,proc=proc)
        while proc.poll() is None:
            if cancelled(jid):kill(proc);proc.wait();raise InterruptedError('已取消，仅停止本任务进程')
            if shutil.disk_usage(ROOT).free<2*2**30:kill(proc);proc.wait();raise RuntimeError('磁盘不足 2 GiB，已停止本任务以保留系统空间')
            if time.monotonic()-started>8*3600:kill(proc);proc.wait();raise RuntimeError('任务生成超过 8 小时')
            progress=None
            for logfile in folder.glob('**/*engine*.log'):
                try:
                    with logfile.open('rb') as f:f.seek(max(0,logfile.stat().st_size-24000));lines=f.read().decode('utf-8','replace').splitlines()
                    for line in reversed(lines):
                        try:r=json.loads(line)
                        except ValueError:continue
                        if r.get('event')=='sampling_progress':
                            done=r.get('completed_steps',0);total=r.get('total_steps',r.get('total',0))
                            if total:progress=min(.94,(float(done)+float(r.get('block',0))/max(1,float(r.get('blocks',1))))/float(total)*.9)
                            break
                except OSError:pass
            update(jid,stage='采样中' if progress else '模型加载 / 编码 / 生成中',**({'progress':progress} if progress is not None else {}));time.sleep(2)
        active.clear()
        if proc.returncode:raise RuntimeError('H3 返回错误 '+str(proc.returncode)+'。展开任务日志查看详情。')
    return folder/'video.mp4'
def req(path,data=None,timeout=15):
    request=urllib.request.Request('http://127.0.0.1:8188'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=timeout) as r:return json.load(r)
def wan_graph(o,folder):
    template=read(ROOT/'workflows/wan-source.json');links={x[0]:x for x in template['links']};graph={}
    mappings={'UNETLoader':['unet_name','weight_dtype'],'CLIPLoader':['clip_name','type','device'],'VAELoader':['vae_name'],'CLIPTextEncode':['text'],'KSampler':['seed','control','steps','cfg','sampler_name','scheduler','denoise'],'ModelSamplingSD3':['shift'],'Wan22ImageToVideoLatent':['width','height','length','batch_size'],'LoadImage':['image','upload'],'CreateVideo':['fps'],'SaveVideo':['filename_prefix','format','codec']}
    for n in template['nodes']:
        typ=n['type']
        if typ=='MarkdownNote':continue
        if typ not in mappings and typ!='VAEDecode':raise ValueError('尚未适配 Wan 节点：'+typ)
        inputs={}
        for key,val in zip(mappings.get(typ,[]),n.get('widgets_values',[])):
            if key not in ['control','upload']:inputs[key]=val
        for inp in n.get('inputs',[]):
            if inp.get('link') is not None:
                link=links[inp['link']];inputs[inp['name']]=[str(link[1]),link[2]]
        if typ=='CLIPTextEncode':inputs['text']=o['negative'] if 'Negative' in n.get('title','') else o['prompt']
        if typ=='KSampler':inputs.update(seed=o['seed'],steps=o['steps'],cfg=o['cfg'])
        if typ=='Wan22ImageToVideoLatent':
            w,h=map(int,o['resolution'].split('x'));inputs.update(width=w,height=h,length=4*math.ceil((o['seconds']*24-1)/4)+1,batch_size=1)
            if not o['first']:inputs.pop('start_image',None)
        if typ=='LoadImage':
            if not o['first']:continue
            src=Path(assetpath(o['first']));name='studio-'+hashlib.sha256(src.read_bytes()).hexdigest()[:20]+src.suffix.lower();dest=COMFY/'input'/name
            if not dest.exists():shutil.copy2(src,dest)
            inputs['image']=name
        if typ=='SaveVideo':inputs.update(filename_prefix='VideoStudio/'+folder.name,format='mp4',codec='h264')
        graph[str(n['id'])]={'class_type':typ,'inputs':inputs}
    write(folder/'api-workflow.json',graph);return graph
def execute_wan(jid,o,folder):
    # The adapter never interrupts global Comfy work; it deletes only its queued prompt.
    try:req('/system_stats')
    except Exception:
        if resource_busy():raise RuntimeError('其他引擎正在运行，请稍后重试 Wan')
        log=(ROOT/'data/comfy.log').open('ab')
        subprocess.Popen([os.environ.get('CATFYUI_COMFY_PYTHON') or str(COMFY/'.venv/Scripts/python.exe'),str(COMFY/'main.py'),'--listen','127.0.0.1','--port','8188','--lowvram','--disable-auto-launch'],cwd=COMFY,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW);log.close()
        for _ in range(90):
            if cancelled(jid):raise InterruptedError('已取消')
            try:req('/system_stats');break
            except Exception:time.sleep(2)
        else:raise RuntimeError('ComfyUI 启动失败，查看 data/comfy.log')
    graph=wan_graph(o,folder); info=req('/object_info')
    absent=[n['class_type'] for n in graph.values() if n['class_type'] not in info]
    if absent:raise RuntimeError('ComfyUI 缺少节点：'+', '.join(absent))
    result=req('/prompt',{'prompt':graph,'client_id':'video-studio-'+jid});write(folder/'comfy-prompt.json',result)
    return receive_wan(jid,folder,result['prompt_id'])
def receive_wan(jid,folder,pid):
    start=time.time()
    while time.time()-start<8*3600:
        hist=req('/history/'+pid)
        if pid in hist:
            row=hist[pid];write(folder/'comfy-result.json',row)
            if row.get('status',{}).get('status_str')=='error':raise RuntimeError('ComfyUI 执行失败，查看 comfy-result.json')
            files=[x for out in row.get('outputs',{}).values() for key in ['videos','gifs','images'] for x in out.get(key,[]) if x.get('filename','').lower().endswith('.mp4')]
            if not files:raise RuntimeError('工作流未返回 MP4')
            src=(COMFY/'output'/files[0].get('subfolder','')/files[0]['filename']).resolve()
            if (COMFY/'output').resolve() not in src.parents:raise RuntimeError('输出路径无效')
            if cancelled(jid):raise InterruptedError('已取消；Comfy 运行结果已留在原目录')
            shutil.copy2(src,folder/'video.mp4');return folder/'video.mp4'
        if cancelled(jid):
            q=req('/queue');running=any(x[1]==pid for x in q.get('queue_running',[]))
            if running:
                update(jid,stage='取消待处理：保留正在执行的 Comfy 任务，结束后不接收结果')
            else:req('/queue',{'delete':[pid]});raise InterruptedError('已从 Comfy 队列取消')
        else:update(jid,stage='ComfyUI 排队 / 生成中')
        time.sleep(3)
    raise RuntimeError('Comfy 等待超时；原 prompt_id 已保存，未自动重复提交')
def worker():
    while not stopped.is_set():
        if setting('paused',False):wake.wait(2);wake.clear();continue
        with db() as c:
            r=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if r and not c.execute("UPDATE jobs SET status='running',stage='准备生成' WHERE id=? AND status='queued' AND cancel=0",(r['id'],)).rowcount:
                r=None
        if not r:wake.wait(2);wake.clear();continue
        jid=r['id'];folder=ROOT/'jobs'/jid;folder.mkdir(exist_ok=True)
        try:
            if cancelled(jid):raise InterruptedError('已取消')
            options=json.loads(r['options'])
            receipt=folder/'comfy-prompt.json'
            if options.get('director_project') and options['workflow']=='wan22' and receipt.is_file():
                output=receive_wan(jid,folder,read(receipt)['prompt_id'])
            else:
                o=validate(options);write(folder/'options.json',o)
                output=execute_wan(jid,o,folder) if o['workflow']=='wan22' else execute_h3(jid,o,folder)
            if cancelled(jid):raise InterruptedError('已取消')
            update(jid,stage='全量解码检查',progress=.96)
            check=subprocess.run([str(FF),'-v','error','-xerror','-i',str(output),'-map','0:v:0','-f','null','-'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if check.returncode:raise RuntimeError('视频解码失败：'+check.stderr.decode('utf-8','replace')[-1000:])
            digest=hashlib.sha256(output.read_bytes()).hexdigest();write(folder/'technical-check.json',{'sha256':digest,'decode':'passed','visual_review':'pending'})
            poster=subprocess.run([str(FF),'-v','error','-y','-ss','0.1','-i',str(output),'-frames:v','1','-vf','scale=480:-2',str(folder/'poster.jpg')],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if poster.returncode or not (folder/'poster.jpg').is_file():raise RuntimeError('封面抽帧失败，MP4 已保留：'+poster.stderr.decode('utf-8','replace')[-1000:])
            if cancelled(jid):raise InterruptedError('已取消；已生成的视频仍保留在任务文件夹')
            update(jid,status='completed',stage='已生成 · 待审片',progress=1,output=str(output))
            scratch=folder/'video.artifacts/rgb.npy'
            if scratch.is_file():scratch.unlink()
        except InterruptedError as e:update(jid,status='cancelled',stage=str(e))
        except Exception as e:
            (folder/'error.log').write_text(traceback.format_exc(),encoding='utf-8');update(jid,status='failed',stage='生成失败',error=str(e))
        finally:active.clear()
def batch_parse(path,base):
    p=Path(path);rows=[]
    if p.suffix.lower()=='.xlsx':
        from openpyxl import load_workbook
        workbook=load_workbook(p,read_only=True,data_only=True)
        try:
            sheets=[list(sheet.iter_rows(max_row=502,values_only=True)) for sheet in workbook]
        finally:
            workbook.close()
    elif p.suffix.lower()=='.csv':
        raw=p.read_bytes()
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:text=raw.decode('gb18030')
        sheets=[list(csv.reader(io.StringIO(text))) ]
    else:raise ValueError('支持 CSV 和 XLSX（不支持旧 XLS）')
    aliases={'prompt':['prompt','提示词','画面提示词','视频提示词'],'title':['title','shot','镜号','分镜','标题'],'first':['first','first_frame','首帧','首帧路径'],'last':['last','last_frame','尾帧','尾帧路径'],'seconds':['seconds','duration','时长'],'resolution':['resolution','分辨率'],'workflow':['workflow','工作流'],'seed':['seed','种子'],'negative':['negative','负面提示词']}
    for si,sheet in enumerate(sheets):
        if not sheet:continue
        headers=[str(x or '').strip().lower() for x in sheet[0]];mapping={k:next((headers.index(a) for a in names if a in headers),None) for k,names in aliases.items()}
        if mapping['prompt'] is None:continue
        for ri,row in enumerate(sheet[1:],2):
            if not any(x is not None and str(x).strip() for x in row):continue
            o=dict(base,candidates=2);error=''
            try:
                for k,i in mapping.items():
                    if i is not None and i<len(row) and row[i] is not None and str(row[i]).strip():o[k]=str(row[i]).strip()
                for k in ['first','last']:
                    if o[k] and len(str(o[k]))!=24:
                        image=Path(o[k]);image=image if image.is_absolute() else Path(setting('asset_root',str(p.parent)))/image
                        o[k]=asset(image)['id']
                o=validate(o)
            except Exception as exc:error=str(exc)
            rows.append({'row':ri,'sheet':si+1,'options':o,'error':error})
    if not rows:raise ValueError('没有找到分镜行。首行需要 prompt / 提示词 列；可下载模板')
    if len(rows)>500:raise ValueError('单次最多导入 500 镜')
    return rows

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def reply(self,obj,code=200):
        body=dumps(obj).encode();self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def safe(self):
        return self.headers.get('Host','') in [f'127.0.0.1:{PORT}',f'localhost:{PORT}'] and self.headers.get('Origin') in [None,URL,f'http://localhost:{PORT}']
    def file(self,path,download=False):
        path=Path(path)
        if not path.is_file():return self.reply({'error':'文件不存在'},404)
        size=path.stat().st_size;start=0;end=size-1;rng=self.headers.get('Range','')
        if rng:
            try:
                a,b=rng.removeprefix('bytes=').split('-');start=int(a) if a else max(0,size-int(b));end=min(int(b),end) if b and a else end
                if start<0 or start>end or start>=size:raise ValueError()
            except ValueError:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
        self.send_response(206 if rng else 200);self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1));self.send_header('Cache-Control','no-cache')
        if rng:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if download:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(path.name))
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start);remain=end-start+1
            while remain:
                chunk=f.read(min(remain,1024*1024))
                if not chunk:break
                self.wfile.write(chunk);remain-=len(chunk)
    def do_GET(self):
        if not self.safe():return self.reply({'error':'只允许本机访问'},403)
        try:
            parts=urllib.parse.urlsplit(self.path);path=parts.path;q=urllib.parse.parse_qs(parts.query)
            if path in ['/','/index.html']:return self.file(ROOT/'index.html')
            if path=='/app.js':return self.file(ROOT/'app.js')
            if path=='/style.css':return self.file(ROOT/'style.css')
            if path=='/agent.js':return self.file(ROOT/'agent.js')
            if path=='/agent.css':return self.file(ROOT/'agent.css')
            if path=='/director.js':return self.file(ROOT/'director.js')
            if path=='/api/films':return self.reply(director().list())
            if path.startswith('/media/films/'):
                pid,kind,filename=path.removeprefix('/media/films/').split('/')
                return self.file(director().artifact(pid,kind,filename),'download' in q)
            if path.startswith('/api/films/') and path.endswith('/package'):
                pid=path.removeprefix('/api/films/').removesuffix('/package')
                with tempfile.TemporaryDirectory(prefix='catfy-film-') as folder:
                    package=Path(folder)/f'CatfyUI-{pid}.zip'
                    director().bundle(pid,package,'media' in q)
                    return self.file(package,True)
            if path.startswith('/api/films/'):
                pid=path.removeprefix('/api/films/')
                return self.reply(director().state(pid, q.get('revision',[None])[0]))
            if path=='/api/agent':return self.reply(agent().state())
            if path=='/api/state':
                with db() as c:items=[dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC,id DESC LIMIT 1000')]
                for r in items:r['options']=json.loads(r['options'])
                return self.reply({'jobs':items,'workflows':workflows(),'paused':setting('paused',False),'drafts':setting('drafts',{}),'asset_root':setting('asset_root',default_asset_root()),'defaults':DEFAULT,'busy':resource_busy(),'url':URL})
            if path=='/api/assets':
                term=q.get('q',[''])[0]
                with db() as c:items=[dict(r) for r in c.execute('SELECT * FROM assets WHERE name LIKE ? ORDER BY name LIMIT 1000',('%'+term+'%',))]
                return self.reply(items)
            if path=='/api/log':
                jid=q['id'][0];r=job(jid);folder=ROOT/'jobs'/jid;tails=[]
                for file in ['generate.log','error.log']:
                    p=folder/file
                    if p.exists():
                        with p.open('rb') as f:f.seek(max(0,p.stat().st_size-24000));tails.append(f.read().decode('utf-8','replace'))
                return self.reply({'text':'\n'.join(tails) or r['stage']})
            if path.startswith('/media/asset/'):return self.file(assetpath(path.split('/')[-1]))
            if path.startswith('/media/job/'):
                _,_,_,jid,kind=path.split('/');r=job(jid)
                if kind=='poster':return self.file(ROOT/'jobs'/jid/'poster.jpg')
                if kind=='options':return self.reply(r['options'])
                if kind=='video' and r['output']:return self.file(r['output'],'download' in q)
                return self.reply({'error':'尚未生成'},404)
            if path=='/template.csv':return self.file(ROOT/'分镜导入模板.csv',True)
            return self.reply({'error':'不存在'},404)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        except Exception as e:self.reply({'error':str(e)},400)
    def do_POST(self):
        if not self.safe():return self.reply({'error':'来源不允许'},403)
        try:
            parts=urllib.parse.urlsplit(self.path);path=parts.path;q=urllib.parse.parse_qs(parts.query);length=int(self.headers.get('Content-Length','0'))
            if path=='/api/upload':
                name=Path(q.get('name',['image.png'])[0].replace('\\','/')).name
                ext=Path(name).suffix.lower()
                if ext not in ['.png','.jpg','.jpeg','.webp','.bmp','.csv','.xlsx'] or not 0<length<80*1024**2:raise ValueError('支持小于 80 MB 的图片 / CSV / XLSX')
                dest=ROOT/'assets'/(uuid.uuid4().hex[:12]+'-'+name);dest.write_bytes(self.rfile.read(length))
                if ext in ['.csv','.xlsx']:return self.reply({'token':dest.name})
                from PIL import Image
                try:
                    with Image.open(dest) as im:im.verify()
                except Exception:dest.unlink();raise ValueError('不是有效的图片文件')
                return self.reply(asset(dest))
            if length>8*1024**2:raise ValueError('请求过大')
            data=json.loads(self.rfile.read(length) or '{}')
            if path=='/api/films/start':return self.reply(director().start(data))
            if path.startswith('/api/films/'):
                pid,action=path.removeprefix('/api/films/').split('/')
                return self.reply(director().action(pid,action,data))
            if path.startswith('/api/agent/'):
                return self.reply(agent().action(path.rsplit('/',1)[-1],data))
            if path=='/api/settings':
                if data.get('workflow') not in [w['id'] for w in workflows()]:raise ValueError('无效工作流草稿')
                with mutex:
                    drafts=setting('drafts',{});drafts[data['workflow']]=data['options'];setsetting('drafts',drafts)
                return self.reply({'ok':True})
            if path=='/api/scan':
                root=Path(data['path']).resolve()
                if not root.is_dir():raise ValueError('文件夹不存在')
                found=[]
                for base,dirs,files in os.walk(root):
                    dirs[:]=[x for x in dirs if x not in ['.git','node_modules','.venv','envs']]
                    for name in files:
                        if Path(name).suffix.lower() in ['.png','.jpg','.jpeg','.webp','.bmp']:
                            found.append(asset(Path(base)/name))
                            if len(found)>=5000:break
                    if len(found)>=5000:break
                setsetting('asset_root',str(root));return self.reply({'count':len(found)})
            if path=='/api/enqueue':
                options=[validate(data['options'])];return self.reply({'ids':enqueue(options)})
            if path=='/api/batch/preview':
                token=data['token']
                if Path(token).name!=token:raise ValueError('无效文件')
                rows=batch_parse(ROOT/'assets'/token,data.get('options',DEFAULT));batchid=uuid.uuid4().hex
                write(ROOT/'data'/(batchid+'.json'),rows);return self.reply({'id':batchid,'rows':rows})
            if path=='/api/batch/submit':
                bid=data['id']
                if len(bid)!=32 or not bid.isalnum():raise ValueError('无效批次')
                file=ROOT/'data'/(bid+'.json');rows=read(file)
                if any(r['error'] for r in rows):raise ValueError('请修正表格内错误后重新导入')
                with mutex:
                    marker=file.with_suffix('.submitted')
                    if marker.exists():raise ValueError('本批已经提交，请勿重复提交')
                    ids=enqueue([validate(r['options']) for r in rows]);marker.write_text(dumps(ids),encoding='utf-8')
                return self.reply({'ids':ids})
            if path=='/api/action':
                action=data['action']
                if action=='pause':setsetting('paused',bool(data['value']));wake.set();return self.reply({'ok':True})
                r=job(data['id'])
                if action=='cancel':
                    if r['status'] in ['queued','running']:
                        update(r['id'],cancel=1,**({'status':'cancelled','stage':'已取消'} if r['status']=='queued' else {}))
                    return self.reply({'ok':True})
                if action=='retry':
                    if r['status'] not in ['failed','interrupted','cancelled']:raise ValueError('仅失败、中断或取消任务可重试')
                    return self.reply({'ids':enqueue([validate(json.loads(r['options']))])})
                if action=='folder':os.startfile(str(ROOT/'jobs'/r['id']));return self.reply({'ok':True})
            if path=='/api/shutdown':
                if active or any(r['status']=='running' for r in self._jobs()):raise ValueError('请先完成或取消正在运行的任务')
                stopped.set();self.reply({'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            return self.reply({'error':'不存在'},404)
        except Exception as e:self.reply({'error':str(e)},400)
    def _jobs(self):
        with db() as c:return [dict(r) for r in c.execute('SELECT status FROM jobs')]
def agent():
    return studio_agent.Agent(sys.modules[__name__])

def director():
    return Director(sys.modules[__name__])

def main():
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler);initialize();print('AI 视频工作台 '+URL,flush=True)
    agent().initialize()
    director().initialize()
    director().recover_jobs()
    threading.Thread(target=worker,daemon=True).start()
    threading.Thread(target=director().loop,daemon=True).start()
    threading.Thread(target=agent().loop,daemon=True).start()
    try:server.serve_forever()
    finally:stopped.set();server.server_close()
if __name__=='__main__':main()
