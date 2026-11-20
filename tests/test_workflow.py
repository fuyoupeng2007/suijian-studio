"""Run with python -m unittest discover -s tests -v. Uses real FFmpeg, no model account."""
import copy
import json
import os
import tempfile
import time
import unittest
import httpx
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT.parents[1] / 'work' / 'verification'
SCRATCH.mkdir(parents=True, exist_ok=True)
TEMP = tempfile.TemporaryDirectory(dir=SCRATCH)
os.environ['SUIJIAN_DATA'] = str(Path(TEMP.name) / 'data')

from fastapi.testclient import TestClient
from server import app as module
from server.media import binary, probe, run
from server.planner import validate_timeline


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)
        cls.client.post('/api/session', json={'code': module.PAIR})
        cls.fixtures = Path(TEMP.name) / 'fixtures'
        cls.fixtures.mkdir()
        cls.video = cls.fixtures / 'with-audio.mp4'
        cls.silent = cls.fixtures / 'silent.mp4'
        cls.image = cls.fixtures / 'photo.png'
        cls.music = cls.fixtures / 'music.wav'
        ff = binary('ffmpeg')
        run([ff,'-v','error','-y','-f','lavfi','-i','testsrc2=s=320x240:r=30:d=3',
             '-f','lavfi','-i','sine=frequency=440:duration=3','-c:v','libx264','-c:a','aac','-shortest',cls.video])
        run([ff,'-v','error','-y','-f','lavfi','-i','color=c=0x8774b6:s=240x320:r=30:d=2',
             '-c:v','libx264','-an',cls.silent])
        run([ff,'-v','error','-y','-f','lavfi','-i','color=c=0xE9D9BE:s=400x300','-frames:v','1',cls.image])
        run([ff,'-v','error','-y','-f','lavfi','-i','sine=frequency=220:duration=2',cls.music])

    def project(self):
        response=self.client.post('/api/projects',json={'name':'周末的片段 · 集成测试'})
        self.assertEqual(response.status_code,200)
        return response.json()

    def upload(self,p,path,kind='visual'):
        with path.open('rb') as f:
            response=self.client.post('/api/projects/'+p['id']+'/media',files={'file':(path.name,f)},data={'kind':kind})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def chat(self,p,text):
        return self.client.post('/api/projects/'+p['id']+'/chat',json={'text':text,'revision':p['revision']})

    def test_01_authentication_and_origin(self):
        stranger=TestClient(module.app)
        self.assertEqual(stranger.get('/api/projects').status_code,401)
        self.assertEqual(stranger.post('/api/session',json={'code':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/projects',json={'name':'x'},headers={'Origin':'https://untrusted.test'}).status_code,403)

    def test_02_import_mixed_media_and_audio(self):
        p=self.project()
        for f,k in [(self.video,'visual'),(self.silent,'visual'),(self.image,'visual'),(self.music,'audio')]:p=self.upload(p,f,k)
        self.assertEqual([m['kind'] for m in p['media']],['video','video','image','audio'])
        self.assertTrue(p['media'][0]['has_audio'])
        self.assertFalse(p['media'][1]['has_audio'])
        self.assertEqual(len(p['media'][0]['frames']),3)

    def test_03_basic_plan_edit_and_undo(self):
        p=self.upload(self.project(),self.video)
        p=self.upload(p,self.image)
        p=self.chat(p,'生成初剪，6秒，竖屏').json()
        self.assertEqual(len(p['timeline']),2)
        before=copy.deepcopy(p['timeline'])
        p=self.chat(p,'第2段改成1秒').json()
        self.assertEqual(p['timeline'][1]['duration'],1)
        response=self.client.post('/api/projects/'+p['id']+'/undo',json={'revision':p['revision']})
        self.assertEqual(response.json()['timeline'],before)

    def test_04_unknown_command_does_not_fabricate_action(self):
        p=self.upload(self.project(),self.video)
        p=self.chat(p,'生成初剪，2秒').json()
        q=self.chat(p,'找出里面最好笑的瞬间').json()
        self.assertEqual(p['timeline'],q['timeline'])
        self.assertIn('暂不理解',q['messages'][-1]['text'])
        self.assertEqual(p['revision'],q['revision'])

    def test_05_bounds_and_nonfinite_values(self):
        p=self.upload(self.project(),self.video)
        for start,length in [(2,3),(-1,1),(0,181),(0,float('nan'))]:
            with self.assertRaises(ValueError):validate_timeline([{'media_id':p['media'][0]['id'],'start':start,'duration':length}],p['media'])
        with self.assertRaises(ValueError):validate_timeline([{'media_id':'invented','duration':1}],p['media'])
        with self.assertRaises(ValueError):validate_timeline(['invalid-shot'],p['media'])

    def test_06_stale_revision_does_not_overwrite(self):
        p=self.upload(self.project(),self.video)
        q=self.chat(p,'生成初剪，2秒').json()
        response=self.chat(p,'全部静音')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.get('/api/projects/'+q['id']).json()['timeline'],q['timeline'])

    def test_07_reject_corrupt_and_unsupported_files(self):
        p=self.project()
        response=self.client.post('/api/projects/'+p['id']+'/media',files={'file':('bad.mp4',b'no video')})
        self.assertEqual(response.status_code,400)
        response=self.client.post('/api/projects/'+p['id']+'/media',files={'file':('script.html',b'<script>')})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/api/projects/'+p['id']).json()['media'],[])

    def test_08_private_config_not_downloadable(self):
        p=self.project()
        self.assertEqual(self.client.get('/api/projects/'+p['id']+'/files/project.json').status_code,404)
        self.assertEqual(self.client.get('/api/projects/'+p['id']+'/files/media/%2E%2E/project.json').status_code,404)

    def test_09_real_render_mixed_media_subtitles_music(self):
        p=self.upload(self.project(),self.video)
        p=self.upload(p,self.silent)
        p=self.upload(p,self.image)
        p=self.upload(p,self.music,'audio')
        p=self.chat(p,'生成初剪，3秒，横屏').json()
        p=self.chat(p,'第2段字幕：今天很开心 {测试}').json()
        response=self.client.put('/api/projects/'+p['id']+'/timeline',json={
            'timeline':p['timeline'],'aspect':p['aspect'],'revision':p['revision'],
            'music_id':p['media'][-1]['id'],'music_volume':.1})
        p=response.json()
        response=self.client.post('/api/projects/'+p['id']+'/render',json={'revision':p['revision'],'quality':'export'})
        self.assertEqual(response.status_code,200,response.text)
        job=response.json()
        for _ in range(240):
            job=self.client.get('/api/jobs/'+job['id']).json()
            if job['status']!='running':break
            time.sleep(.25)
        self.assertEqual(job['status'],'done',job.get('message'))
        result=probe(module.DATA/p['id']/job['export']['file'])
        self.assertEqual((result['width'],result['height']),(1280,720))
        self.assertAlmostEqual(result['duration'],3,delta=.2)
        self.assertTrue(result['has_audio'])
        download=self.client.get('/api/projects/'+p['id']+'/export/'+job['id'])
        self.assertEqual(download.status_code,200)
        self.assertGreater(len(download.content),1000)
        (SCRATCH/'verified-film.mp4').write_bytes(download.content)
        (SCRATCH/'render-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')

    def test_10_model_failure_keeps_timeline(self):
        p=self.upload(self.project(),self.video)
        p=self.chat(p,'生成初剪，1秒').json()
        with patch.object(module,'settings',return_value={'api_key':'test','model':'test','base_url':'https://example.invalid/v1'}):
            async def fail(*args):raise ValueError('模型服务不可用')
            with patch.object(module,'model_edit',side_effect=fail):
                response=self.chat(p,'换个镜头')
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/api/projects/'+p['id']).json()['timeline'],p['timeline'])

    def test_11_pair_cookie_is_httponly(self):
        response=self.client.post('/api/session',json={'code':module.PAIR})
        cookie=response.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie)
        self.assertIn('samesite=strict',cookie)

    def test_12_manual_reorder_and_mute(self):
        p=self.upload(self.project(),self.video)
        p=self.upload(p,self.image)
        p=self.chat(p,'生成初剪，2秒').json()
        timeline=list(reversed(p['timeline']))
        timeline[0]['volume']=0
        response=self.client.put('/api/projects/'+p['id']+'/timeline',json={
            'timeline':timeline,'aspect':'1:1','revision':p['revision'],'music_id':None,'music_volume':.18})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['timeline'][0]['media_id'],p['media'][1]['id'])
        self.assertEqual(response.json()['timeline'][0]['volume'],0)

    def test_13_model_adapter_sends_frames_and_validates_plan(self):
        p=self.upload(self.project(),self.video)
        desired=[{'media_id':p['media'][0]['id'],'start':.5,'duration':1,'volume':1,'caption':'测试字幕','reason':'模拟模型方案',
                  'transition':{'type':'cut','duration':0},'look':'natural','motion':'none'}]
        captured=[]
        def handler(request):
            body=json.loads(request.content);captured.append(body)
            text=body['messages'][0]['content'][0]['text']
            if '你是视频素材研究员' in text:
                data=json.loads(text.split('\n',1)[1].split('\n返回此 JSON Schema',1)[0]);ev=data['evidence'][0]
                result={'media':[{'media_id':p['media'][0]['id'],'summary':'模拟视觉服务：测试图案',
                    'subjects':['测试图案'],'setting':'未知','events':[{'start':ev['time'],'end':ev['time'],
                    'description':'采样时刻可见测试图案','panels':[ev['panel']],'confidence':'high'}],
                    'camera_and_light':'未作真实语义判断','editorial_roles':['测试'],'uncertainties':['模拟服务只验证协议']}],
                    'relations':[],'story_outline':[],'limitations':['这是模型接口模拟测试']}
            else:
                result={'timeline':desired,'aspect':'9:16','message':'模拟模型已规划','editorial_plan':{
                    'theme':'测试','structure':['测试片段'],'pace':'平稳','style':'自然',
                    'transition_strategy':'直接切换','audio_strategy':'保留原声','uncertainties':[]}}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})
        original=httpx.AsyncClient
        config={'api_key':'not-a-real-key','model':'mock-vision','base_url':'https://model.test/v1','send_frames':True}
        with patch.object(module,'settings',return_value=config), patch('server.planner.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
            response=self.chat(p,'截取中间一秒')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['timeline'],desired)
        self.assertEqual(len(captured),2)
        content=captured[0]['messages'][0]['content']
        self.assertEqual(len([x for x in content if x['type']=='image_url']),1)
        self.assertNotIn(p['media'][0]['file'],content[0]['text'])
        planning=captured[1]['messages'][0]['content'][0]['text']
        self.assertIn('model_max_gap',planning)
        self.assertIn('测试图案',planning)
        self.assertTrue(response.json()['understanding']['evidence'])
        self.assertEqual(response.json()['editorial_plan']['theme'],'测试')
        self.assertEqual(response.json()['messages'][-1]['mode'],'model')

    def test_14_remote_plaintext_model_and_bad_aspect_rejected(self):
        response=self.client.put('/api/settings',json={'base_url':'http://remote.test/v1','model':'x','api_key':'fake'})
        self.assertEqual(response.status_code,400)
        p=self.upload(self.project(),self.video)
        p=self.chat(p,'生成初剪，1秒').json()
        response=self.client.put('/api/projects/'+p['id']+'/timeline',json={
            'timeline':p['timeline'],'aspect':'bad','revision':p['revision']})
        self.assertEqual(response.status_code,400)

    def test_15_project_list_has_empty_and_effective_durations(self):
        p=self.project()
        response=self.client.get('/api/projects')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(next(x for x in response.json() if x['id']==p['id'])['duration'],0)
        p=self.upload(p,self.video);p=self.upload(p,self.image)
        clips=[{'media_id':m['id'],'start':0,'duration':1,'volume':1} for m in p['media']]
        clips[1]['transition']={'type':'dissolve','duration':.4}
        response=self.client.put('/api/projects/'+p['id']+'/timeline',json={
            'timeline':clips,'aspect':'16:9','revision':p['revision']})
        self.assertEqual(response.status_code,200,response.text)
        listing=self.client.get('/api/projects').json()
        self.assertEqual(next(x for x in listing if x['id']==p['id'])['duration'],1.6)
        saved=module.read_project(p['id']);saved['understanding']={'cached':'old visual evidence'}
        saved['editorial_plan']={'theme':'old theme'};module.write_project(saved)
        p=self.upload(p,self.silent)
        self.assertNotIn('understanding',p)
        self.assertNotIn('editorial_plan',p)


if __name__=='__main__':unittest.main(verbosity=2)
