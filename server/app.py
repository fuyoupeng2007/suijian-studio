import asyncio
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .media import probe, render, thumbnail
from .planner import basic_edit, model_edit, validate_timeline
from .codex_agent import codex_edit, codex_path
from .intelligence import analyze_project, create_edit
from .effects import timeline_duration

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('SUIJIAN_DATA', ROOT / '.local')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
PAIR_FILE = DATA / 'pair-code.txt'
if not PAIR_FILE.exists():
    PAIR_FILE.write_text(secrets.token_urlsafe(24), encoding='utf-8')
PAIR = PAIR_FILE.read_text(encoding='utf-8').strip()
SESSION = hmac.new(PAIR.encode(), b'suijian-local-session', hashlib.sha256).hexdigest()
LOCK = threading.RLock()
JOBS = {}
ATTEMPTS = {}
WORKERS = ThreadPoolExecutor(max_workers=2)
ACTIVE = set()
INTELLIGENCE_ACTIVE=set()
AGENT_JOBS = {}
AGENT_TASKS = set()
app = FastAPI(title='随剪 / Suijian', docs_url=None, redoc_url=None, openapi_url=None)


def uid():
    return uuid.uuid4().hex


def folder_for(project_id):
    if len(project_id) != 32 or any(c not in '0123456789abcdef' for c in project_id):
        raise HTTPException(404, '项目不存在')
    folder = DATA / project_id
    if not (folder / 'project.json').exists():
        raise HTTPException(404, '项目不存在')
    return folder


def read_project(project_id):
    with LOCK:
        return json.loads((folder_for(project_id) / 'project.json').read_text(encoding='utf-8'))


def write_project(project):
    with LOCK:
        path = DATA / project['id'] / 'project.json'
        temp = path.with_suffix('.tmp')
        stored = {k: v for k, v in project.items() if k != 'active_agent_job'}
        temp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)


def public_project(project):
    result = {k: v for k, v in project.items() if k not in ('history', 'active_agent_job')}
    with LOCK:
        result['active_agent_job'] = next((copy.deepcopy(job) for job in AGENT_JOBS.values()
            if job['project_id'] == project['id'] and job['status'] in ('queued', 'running')), None)
    return result


def settings():
    path = DATA / 'model-settings.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'provider':'codex' if os.environ.get('SUIJIAN_CODEX_DEFAULT')=='1' else 'basic'}


def save_edit(project, timeline, aspect, message, mode, plan=None, understanding=None):
    timeline = validate_timeline(timeline, project['media'])
    if timeline != project['timeline'] or aspect != project['aspect']:
        project['history'].append({k: copy.deepcopy(project[k]) for k in ('timeline', 'aspect', 'music_id', 'music_volume')})
        project['history'] = project['history'][-20:]
        project['timeline'] = timeline
        project['aspect'] = aspect
        project['revision'] += 1
    project['messages'].append({'role': 'assistant', 'text': message, 'mode': mode})
    if plan is not None:
        project['editorial_plan']=plan
        project['plan_revision']=project['revision']
    if understanding is not None:project['understanding']=understanding
    write_project(project)
    return public_project(project)


@app.middleware('http')
async def access_control(request: Request, call_next):
    if request.url.path.startswith('/api/') and request.url.path != '/api/session':
        if not hmac.compare_digest(request.cookies.get('suijian_session', ''), SESSION):
            return JSONResponse({'detail': '请先输入电脑端显示的配对码'}, status_code=401)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


class PairRequest(BaseModel):
    code: str = Field(max_length=200)


@app.post('/api/session')
def pair(body: PairRequest, request: Request):
    ip = request.client.host
    now = time.time()
    with LOCK:
        attempts = [t for t in ATTEMPTS.get(ip, []) if now - t < 60]
        if len(attempts) >= 8:
            raise HTTPException(429, '尝试过多，请一分钟后重试')
        attempts.append(now)
        ATTEMPTS[ip] = attempts
    if not hmac.compare_digest(body.code, PAIR):
        raise HTTPException(401, '配对码不正确')
    response = JSONResponse({'ok': True})
    response.set_cookie('suijian_session', SESSION, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https', max_age=86400 * 30)
    return response


@app.delete('/api/session')
def logout():
    response = JSONResponse({'ok': True})
    response.delete_cookie('suijian_session')
    return response


@app.get('/api/status')
def status():
    config = settings()
    provider=config.get('provider','api' if config.get('api_key') else 'basic')
    return {'name': '随剪 Studio', 'version': '0.5.0', 'mode': 'model' if provider=='codex' or (provider=='api' and config.get('api_key')) else 'basic',
            'provider':provider,'codex_available':bool(codex_path()),
            'model': config.get('model', ''), 'base_url': config.get('base_url', ''),
            'send_frames': config.get('send_frames', False), 'data_location': str(DATA)}


@app.get('/healthz')
def health():
    return {'app': 'suijian-studio', 'version': '0.5.0'}


class ModelSettings(BaseModel):
    provider: str = 'api'
    base_url: str = Field(default='', max_length=500)
    model: str = Field(default='', max_length=120)
    api_key: str = Field(default='', max_length=1000)
    send_frames: bool = False
    clear_key: bool = False


@app.put('/api/settings')
def update_settings(body: ModelSettings):
    if body.provider not in ('api','codex','basic'):
        raise ValueError('未知模型模式')
    parsed = urlparse(body.base_url)
    if body.base_url and (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment):
        raise ValueError('请输入有效模型地址，例如 https://服务域名/v1')
    if body.base_url and parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('远程模型接口必须使用 HTTPS；本机模型可使用 HTTP')
    old = settings()
    key = '' if body.clear_key else (body.api_key or old.get('api_key', ''))
    if key and (not body.model.strip() or not body.base_url):
        raise ValueError('请同时填写接口地址与模型名称')
    config = {'base_url': body.base_url.rstrip('/'), 'model': body.model.strip(),
              'api_key': key, 'send_frames': body.send_frames,'provider':'basic' if body.clear_key else body.provider}
    with LOCK:
        (DATA / 'model-settings.json').write_text(json.dumps(config), encoding='utf-8')
    return {'ok': True, 'mode': 'model' if config['provider']=='codex' or (config['provider']=='api' and key) else 'basic'}


@app.get('/api/projects')
def list_projects():
    with LOCK:
        items = []
        for path in DATA.glob('*/project.json'):
            p = json.loads(path.read_text(encoding='utf-8'))
            items.append({'id': p['id'], 'name': p['name'], 'created': p['created'],
                          'count': len([m for m in p['media'] if m['kind'] != 'audio']),
                          'duration': round(timeline_duration(p['timeline']), 1) if p['timeline'] else 0})
    return sorted(items, key=lambda p: p['created'], reverse=True)


class CreateProject(BaseModel):
    name: str = Field(default='我的生活片段', min_length=1, max_length=80)


@app.post('/api/projects')
def create_project(body: CreateProject):
    ident = uid()
    (DATA / ident / 'media').mkdir(parents=True)
    project = {'id': ident, 'name': body.name.strip() or '我的生活片段', 'created': time.time(),
               'media': [], 'timeline': [], 'aspect': '9:16', 'revision': 0, 'history': [],
               'music_id': None, 'music_volume': .18, 'exports': [],
               'messages': [{'role': 'assistant', 'text': '先导入照片或视频，再告诉我你想怎么剪。', 'mode': 'basic'}]}
    write_project(project)
    return public_project(project)


@app.get('/api/projects/{project_id}')
def get_project(project_id: str):
    return public_project(read_project(project_id))


def ingest_media(project_id, temp, original_name, kind):
    folder = folder_for(project_id)
    info = probe(temp)
    if kind == 'audio' and not info['has_audio']:
        raise ValueError('所选文件不含音轨')
    if kind != 'audio' and not info['has_video']:
        raise ValueError('请选择可读取的照片或视频')
    if kind == 'video' and (info['duration'] <= 0 or info['duration'] > 3600):
        raise ValueError('单个视频需在 0—60 分钟之间')
    ident = uid()
    final = folder / 'media' / (ident + temp.suffix)
    temp.replace(final)
    item = {'id': ident, 'name': original_name[:160], 'kind': kind, **info,
            'file': final.relative_to(folder).as_posix(), 'frames': []}
    if kind != 'audio':
        times = [0] if kind == 'image' else [round(info['duration'] * f, 2) for f in (.1, .5, .85)]
        for moment in times:
            frame = folder / 'media' / f'{ident}-at-{moment}.jpg'
            thumbnail(final, frame, kind == 'image', moment)
            item['frames'].append(frame.relative_to(folder).as_posix())
        item['thumbnail'] = item['frames'][0]
    with LOCK:
        project = read_project(project_id)
        if len(project['media']) >= 40:
            raise ValueError('每个项目最多导入 40 份素材')
        project['media'].append(item)
        if kind != 'audio':
            project.pop('understanding', None)
            project.pop('editorial_plan', None)
            project.pop('plan_revision', None)
        write_project(project)
    return public_project(project)


@app.post('/api/projects/{project_id}/media')
async def upload_media(project_id: str, file: UploadFile = File(...), kind: str = Form('visual')):
    folder = folder_for(project_id)
    if len(read_project(project_id)['media']) >= 40:
        raise ValueError('每个项目最多导入 40 份素材')
    ext = Path(file.filename or '').suffix.lower()
    if kind == 'audio':
        if ext not in ('.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac'):
            raise ValueError('配乐支持 MP3、WAV、M4A、AAC、OGG、FLAC')
        media_kind = 'audio'
    elif ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
        media_kind = 'image'
    elif ext in ('.mp4', '.mov', '.m4v', '.mkv', '.webm', '.3gp'):
        media_kind = 'video'
    else:
        raise ValueError('暂不支持此格式。HEIC 照片请先转为 JPG；视频推荐 MP4/MOV')
    temp = folder / 'media' / (uid() + ext)
    total = 0
    try:
        with temp.open('wb') as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    raise ValueError('单个文件不能超过 500 MB')
                output.write(chunk)
        return await asyncio.to_thread(ingest_media, project_id, temp, file.filename or '素材', media_kind)
    finally:
        temp.unlink(missing_ok=True)
        await file.close()


@app.get('/api/projects/{project_id}/files/{relative:path}')
def get_file(project_id: str, relative: str):
    folder = folder_for(project_id)
    path = (folder / relative).resolve()
    allowed = path.is_relative_to(folder / 'media') or path.is_relative_to(folder / 'renders')
    if not allowed or not path.is_file() or path.suffix.lower() not in ('.mp4', '.jpg', '.jpeg', '.png', '.webp', '.mov', '.m4v', '.webm', '.3gp', '.mkv', '.bmp', '.mp3', '.m4a', '.wav', '.aac', '.ogg', '.flac'):
        raise HTTPException(404, '文件不存在')
    return FileResponse(path)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    duration: float = Field(default=30, ge=1, le=180)
    revision: int


@app.post('/api/projects/{project_id}/analyze')
async def analyze(project_id: str):
    with LOCK:
        p=read_project(project_id)
        if project_id in INTELLIGENCE_ACTIVE:raise HTTPException(409,'该项目的智能体正在处理，请稍候')
        INTELLIGENCE_ACTIVE.add(project_id)
    try:
        understanding=await analyze_project(p,folder_for(project_id),settings())
        with LOCK:
            current=read_project(project_id)
            if [m['id'] for m in current['media']]!=[m['id'] for m in p['media']]:
                raise HTTPException(409,'分析期间素材发生变化，请重新分析')
            current['understanding']=understanding;write_project(current)
            return public_project(current)
    finally:
        with LOCK:INTELLIGENCE_ACTIVE.discard(project_id)


@app.post('/api/projects/{project_id}/chat')
async def chat(project_id: str, body: ChatRequest):
    p = read_project(project_id)
    if body.revision != p['revision']:
        raise HTTPException(409, '项目已改变，请刷新后再试')
    config = settings()
    with LOCK:
        if project_id in INTELLIGENCE_ACTIVE:raise HTTPException(409,'该项目的智能体正在处理，请稍候')
        INTELLIGENCE_ACTIVE.add(project_id)
    model_project={};understanding=None
    try:
        visual=config.get('provider')=='codex' or (config.get('provider','api')=='api' and config.get('api_key') and config.get('send_frames'))
        if visual:understanding=await analyze_project(p,folder_for(project_id),config)
        if config.get('provider')=='codex' or (config.get('api_key') and config.get('provider','api')=='api'):
            model_project={k:copy.deepcopy(p[k]) for k in ('media','timeline','aspect')}
            for m in model_project['media']:
                m.pop('file',None);m.pop('thumbnail',None)
            model_project['recent_requests']=[m['text'] for m in p['messages'] if m['role']=='user'][-6:]
            if understanding:model_project['understanding']=understanding
            if config.get('provider')=='codex':
                timeline,aspect,message=await codex_edit(model_project,body.text,folder_for(project_id))
            else:timeline,aspect,message=await model_edit(model_project,body.text,config,folder_for(project_id))
            mode='model'
        else:
            timeline,aspect,message=basic_edit(p,body.text,body.duration);mode='basic'
    finally:
        with LOCK:INTELLIGENCE_ACTIVE.discard(project_id)
    with LOCK:
        current = read_project(project_id)
        if current['revision'] != body.revision or len(current['media']) != len(p['media']):
            raise HTTPException(409, '处理期间项目发生变化，方案未应用，请重试')
        current['messages'].append({'role': 'user', 'text': body.text})
        return save_edit(current, timeline, aspect, message, mode, model_project.get('editorial_plan'),understanding)


class AgentJobRequest(BaseModel):
    kind: Literal['analyze', 'edit']
    revision: int = Field(ge=0)
    text: str = Field(default='', max_length=2000)
    duration: float = Field(default=30, ge=1, le=180)


def ensure_agent_snapshot(current, snapshot):
    if (current['revision'] != snapshot['revision'] or
            [m['id'] for m in current['media']] != [m['id'] for m in snapshot['media']]):
        raise HTTPException(409, '处理期间项目或素材发生变化，方案未应用，请核对后重试')


async def agent_worker(snapshot, config, job, duration):
    """Run independently of the request connection; milestones never advance on a timer."""
    def progress(stage, percent, message):
        with LOCK:
            if job['status'] not in ('queued', 'running'):
                return
            job.update(status='running', stage=stage, progress=max(job['progress'], min(99, int(percent))),
                       message=str(message)[:500], updated=time.time())

    # Let the 202 response be sent before doing any local work.
    await asyncio.sleep(0)
    try:
        progress('preparing', 1, '正在检查素材与剪辑要求')
        project_id = snapshot['id']
        folder = folder_for(project_id)
        model_project = {}
        understanding = None
        if job['kind'] == 'analyze':
            understanding = await analyze_project(snapshot, folder, config, on_progress=progress)
        else:
            visual = config.get('provider') == 'codex' or (
                config.get('provider', 'api') == 'api' and config.get('api_key') and config.get('send_frames'))
            if visual:
                understanding = await analyze_project(snapshot, folder, config, on_progress=progress)
            if config.get('provider') == 'codex' or (
                    config.get('provider', 'api') == 'api' and config.get('api_key')):
                model_project = {k: copy.deepcopy(snapshot[k]) for k in ('media', 'timeline', 'aspect')}
                for material in model_project['media']:
                    material.pop('file', None)
                    material.pop('thumbnail', None)
                model_project['recent_requests'] = [m['text'] for m in snapshot['messages'] if m['role'] == 'user'][-6:]
                if understanding:
                    model_project['understanding'] = understanding
                    timeline, aspect, message = await create_edit(
                        model_project, job['request_text'], folder, config, on_progress=progress)
                else:
                    progress('planning', 65, '文字模型正在规划剪辑；当前未启用画面分析')
                    timeline, aspect, message = await model_edit(model_project, job['request_text'], config, folder)
                mode = 'model'
            else:
                progress('planning', 65, '正在按基础规则组织镜头；当前未启用画面分析')
                timeline, aspect, message = basic_edit(snapshot, job['request_text'], duration)
                mode = 'basic'
        progress('applying', 90, '正在校验项目版本并保存结果')
        with LOCK:
            current = read_project(snapshot['id'])
            ensure_agent_snapshot(current, snapshot)
            if job['kind'] == 'analyze':
                current['understanding'] = understanding
                write_project(current)
            else:
                current['messages'].append({'role': 'user', 'text': job['request_text']})
                save_edit(current, timeline, aspect, message, mode, model_project.get('editorial_plan'), understanding)
            job.update(status='done', stage='done', progress=100, message='素材理解已完成' if job['kind'] == 'analyze' else '剪辑方案已保存到时间线',
                       result_revision=current['revision'], updated=time.time())
    except asyncio.CancelledError:
        with LOCK:
            job.update(status='failed', stage='failed', message='任务已中断，原时间线已保留',
                       error='本机服务停止，任务未完成；请重新提交', updated=time.time())
        raise
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        with LOCK:
            job.update(status='failed', stage='failed', message='任务未完成，原时间线已保留',
                       error=str(detail or '智能体处理失败')[:2400], updated=time.time())
    finally:
        with LOCK:
            INTELLIGENCE_ACTIVE.discard(snapshot['id'])


@app.post('/api/projects/{project_id}/agent-jobs', status_code=202)
async def start_agent_job(project_id: str, body: AgentJobRequest):
    if body.kind == 'edit' and not body.text.strip():
        raise ValueError('请先输入剪辑要求')
    with LOCK:
        snapshot = read_project(project_id)
        if snapshot['revision'] != body.revision:
            raise HTTPException(409, '项目已改变，请刷新后再试')
        if project_id in INTELLIGENCE_ACTIVE:
            raise HTTPException(409, '该项目的智能体正在处理，请稍候')
        if not any(m['kind'] != 'audio' for m in snapshot['media']):
            raise ValueError('请先导入照片或视频')
        config = settings()
        now = time.time()
        job = {'id': uid(), 'project_id': project_id, 'kind': body.kind, 'revision': body.revision,
               'status': 'queued', 'stage': 'queued', 'progress': 0, 'message': '任务已提交，准备处理',
               'request_text': body.text.strip(), 'created': now, 'updated': now}
        AGENT_JOBS[job['id']] = job
        INTELLIGENCE_ACTIVE.add(project_id)
        # Keep bounded terminal metadata; active tasks are never evicted.
        finished = [ident for ident, old in AGENT_JOBS.items() if old['status'] in ('done', 'failed')]
        for ident in finished[:max(0, len(AGENT_JOBS) - 200)]:
            AGENT_JOBS.pop(ident, None)
        response = copy.deepcopy(job)
    task = asyncio.create_task(agent_worker(copy.deepcopy(snapshot), copy.deepcopy(config), job, body.duration))
    tasks = AGENT_TASKS
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return response


@app.get('/api/agent-jobs/{job_id}')
def get_agent_job(job_id: str):
    with LOCK:
        if job_id not in AGENT_JOBS:
            raise HTTPException(404, '智能体任务不存在；本机服务重启后请重新提交')
        return copy.deepcopy(AGENT_JOBS[job_id])


class TimelineRequest(BaseModel):
    timeline: list
    aspect: str
    revision: int
    music_id: str | None = None
    music_volume: float = Field(default=.18, ge=0, le=1)


@app.put('/api/projects/{project_id}/timeline')
def edit_timeline(project_id: str, body: TimelineRequest):
    with LOCK:
        p = read_project(project_id)
        if body.revision != p['revision']:
            raise HTTPException(409, '项目已改变，请刷新后再试')
        if body.aspect not in ('9:16', '16:9', '1:1'):
            raise ValueError('画幅无效')
        if body.music_id and not any(m['id'] == body.music_id and m['kind'] == 'audio' for m in p['media']):
            raise ValueError('配乐素材不存在')
        timeline = validate_timeline(body.timeline, p['media'])
        p['history'].append({k: copy.deepcopy(p[k]) for k in ('timeline', 'aspect', 'music_id', 'music_volume')})
        p['history'] = p['history'][-20:]
        p.update(timeline=timeline, aspect=body.aspect, music_id=body.music_id, music_volume=body.music_volume)
        p['revision'] += 1
        write_project(p)
        return public_project(p)


class RevisionRequest(BaseModel):
    revision: int


@app.post('/api/projects/{project_id}/undo')
def undo(project_id: str, body: RevisionRequest):
    with LOCK:
        p = read_project(project_id)
        if body.revision != p['revision']:
            raise HTTPException(409, '项目已改变，请刷新后再试')
        if not p['history']:
            raise ValueError('没有可以撤销的修改')
        p.update(p['history'].pop())
        p['revision'] += 1
        p['messages'].append({'role': 'assistant', 'text': '已恢复上一次时间线。', 'mode': 'basic'})
        write_project(p)
        return public_project(p)


class RenderRequest(BaseModel):
    revision: int
    quality: str = 'preview'


def render_worker(project, job):
    def progress(percent, message):
        with LOCK:
            job.update(progress=percent, message=message)
    try:
        relative, info = render(project, folder_for(project['id']), job['id'], progress, job['quality'])
        export = {'id': job['id'], 'file': relative, 'revision': project['revision'],
                  'quality': job['quality'], 'info': info, 'created': time.time()}
        with LOCK:
            p = read_project(project['id'])
            p['exports'].append(export)
            write_project(p)
            job.update(status='done', export=export)
    except Exception as exc:
        with LOCK:
            job.update(status='failed', message=str(exc)[:2400])
    finally:
        with LOCK:
            ACTIVE.discard(project['id'])


@app.post('/api/projects/{project_id}/render')
def start_render(project_id: str, body: RenderRequest):
    with LOCK:
        p = read_project(project_id)
        if body.revision != p['revision']:
            raise HTTPException(409, '项目已改变，请刷新后再试')
        p['timeline'] = validate_timeline(p['timeline'], p['media'])
        if body.quality not in ('preview', 'export'):
            raise ValueError('请选择预览或导出')
        if project_id in ACTIVE or len(ACTIVE) >= 2:
            raise HTTPException(409, '已有任务正在处理，请完成后重试')
        job = {'id': uid(), 'project_id': project_id, 'revision': p['revision'], 'quality': body.quality,
               'progress': 0, 'message': '准备剪辑', 'status': 'running'}
        JOBS[job['id']] = job
        ACTIVE.add(project_id)
        WORKERS.submit(render_worker, copy.deepcopy(p), job)
    return copy.deepcopy(job)


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    with LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, '任务不存在；服务重启后请重新生成')
        return copy.deepcopy(JOBS[job_id])


@app.get('/api/projects/{project_id}/export/{export_id}')
def download_export(project_id: str, export_id: str):
    p = read_project(project_id)
    export = next((e for e in p['exports'] if e['id'] == export_id), None)
    if not export:
        raise HTTPException(404, '成片不存在')
    return FileResponse(folder_for(project_id) / export['file'], media_type='video/mp4',
                        filename=f'suijian-{export_id[:8]}.mp4')


app.mount('/', StaticFiles(directory=ROOT / 'web', html=True), name='web')
