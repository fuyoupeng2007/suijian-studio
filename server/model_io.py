"""Bounded model JSON calls; authenticated CLI or an explicitly configured API."""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
import httpx


def decode_json(raw):
    if not isinstance(raw,str):raise ValueError('模型须返回 JSON 文本')
    value=raw.strip()
    if value.startswith('```'):
        value=value.split('\n',1)[1].rsplit('```',1)[0]
    obj=json.loads(value)
    if not isinstance(obj,dict):raise ValueError('模型须返回 JSON 对象')
    return obj


def codex_json(prompt,schema,images,run_dir):
    executable=shutil.which('codex.exe') or shutil.which('codex')
    if not executable:raise ValueError('请先安装并登录 Codex，或在设置中配置视觉模型 API')
    run_dir.mkdir(parents=True,exist_ok=True)
    schema_path=run_dir/'schema.json';output=run_dir/'result.json'
    schema_path.write_text(json.dumps(schema),encoding='utf8')
    env=os.environ.copy()
    for protocol,value in urllib.request.getproxies().items():
        if protocol in ('http','https'):env.setdefault(protocol.upper()+'_PROXY',value)
    command=[executable,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
             '--sandbox','read-only','-C',str(run_dir),'--output-schema',str(schema_path),'-o',str(output)]
    for image in images:command+=['-i',str(image)]
    command+=['-']
    try:
        result=subprocess.run(command,input=prompt.encode('utf8'),capture_output=True,timeout=300,env=env,
                              creationflags=0x08000000 if os.name=='nt' else 0)
    except subprocess.TimeoutExpired as exc:raise ValueError('模型分析超过5分钟，请检查网络或减少素材；原时间线未修改') from exc
    if result.returncode or not output.exists():raise ValueError('Codex 未返回有效结果，请检查登录和网络；原时间线未修改')
    try:return decode_json(output.read_text(encoding='utf8'))
    except (json.JSONDecodeError,IndexError) as exc:raise ValueError('模型返回格式错误，原时间线未修改') from exc


async def call_json(config,prompt,schema,images,run_dir):
    if config.get('provider')=='codex':
        return await asyncio.to_thread(codex_json,prompt,schema,images,run_dir)
    if not config.get('api_key'):raise ValueError('请配置视觉模型 API 或使用本机 Codex')
    content=[{'type':'text','text':prompt+'\n返回此 JSON Schema 对应的对象：'+json.dumps(schema,ensure_ascii=False)}]
    for image in images:
        content.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(image.read_bytes()).decode()}})
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            result=await client.post(config['base_url'].rstrip('/')+'/chat/completions',
                headers={'Authorization':'Bearer '+config['api_key']},
                json={'model':config['model'],'messages':[{'role':'user','content':content}],
                      'temperature':.2,'max_tokens':12000})
    except httpx.RequestError as exc:raise ValueError('模型服务连接失败或超时；原时间线未修改') from exc
    if result.status_code>=400:raise ValueError(f'模型服务返回 HTTP {result.status_code}，原时间线未修改')
    try:
        obj=decode_json(result.json()['choices'][0]['message']['content'])
        run_dir.mkdir(parents=True,exist_ok=True)
        (run_dir/'result.json').write_text(json.dumps(obj,ensure_ascii=False),encoding='utf8')
        return obj
    except (KeyError,TypeError,json.JSONDecodeError,IndexError) as exc:raise ValueError('模型结果格式错误，原时间线未修改') from exc
