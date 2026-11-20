"""Background API lifecycle checks. Model calls are gated/mocked; no account is used."""
import asyncio
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class AgentJobs(unittest.TestCase):
    def setUp(self):
        from server import app as module
        self.module=module
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)
        for name,value in [('DATA',self.data),('AGENT_JOBS',{}),('AGENT_TASKS',set()),
                           ('INTELLIGENCE_ACTIVE',set()),('ATTEMPTS',{})]:
            patched=patch.object(module,name,value);patched.start();self.addCleanup(patched.stop)
        self.config=patch.object(module,'settings',return_value={'provider':'codex'})
        self.config.start();self.addCleanup(self.config.stop)
        self.client=TestClient(module.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.client.post('/api/session',json={'code':module.PAIR}).raise_for_status()
        self.entered=threading.Event();self.release=threading.Event()
        self.addCleanup(self.release.set)
        self.project=self.client.post('/api/projects',json={'name':'Agent job test'}).json()
        saved=module.read_project(self.project['id'])
        saved['media']=[{'id':'source','kind':'video','name':'test.mp4','duration':5,
                         'file':'media/test.mp4','frames':[],'has_audio':False,'width':320,'height':240}]
        saved['timeline']=[{'media_id':'source','start':0,'duration':1,'volume':1,'caption':'existing','reason':'old plan'}]
        saved['revision']=1
        module.write_project(saved)
        self.project=self.client.get('/api/projects/'+saved['id']).json()

    async def slow_analysis(self,project,folder,config,on_progress=None):
        if on_progress:on_progress('visual',45,'视觉模型正在理解画面内容与素材联系')
        self.entered.set()
        while not self.release.is_set():await asyncio.sleep(.005)
        return {'media':[{'media_id':'source','summary':'mocked visual evidence'}],
                'relations':[],'evidence':[],'coverage':[],'limitations':['mock model only']}

    def submit(self,kind='analyze',**overrides):
        body={'kind':kind,'revision':self.project['revision'],'text':'Cut a short story'}
        body.update(overrides)
        return self.client.post('/api/projects/'+self.project['id']+'/agent-jobs',json=body)

    def wait_job(self,ident):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            response=self.client.get('/api/agent-jobs/'+ident)
            self.assertEqual(response.status_code,200,response.text)
            job=response.json()
            if job['status'] in ('done','failed'):return job
            time.sleep(.01)
        self.fail('background job failed to finish')

    def test_202_returns_while_model_waits_and_refresh_discovers_the_same_job(self):
        with patch.object(self.module,'analyze_project',side_effect=self.slow_analysis):
            started=time.monotonic();response=self.submit()
            self.assertEqual(response.status_code,202,response.text)
            self.assertLess(time.monotonic()-started,2)
            self.assertFalse(self.release.is_set())
            self.assertTrue(self.entered.wait(2))
            ident=response.json()['id']
            # Another authenticated browser can recover the task without the original connection.
            with TestClient(self.module.app) as refreshed:
                refreshed.cookies.update(self.client.cookies)
                observed=refreshed.get('/api/projects/'+self.project['id']).json()['active_agent_job']
                self.assertEqual(observed['id'],ident)
                self.assertEqual(observed['stage'],'visual')
                self.assertEqual(observed['progress'],45)
                self.assertEqual(observed['request_text'],'Cut a short story')
                time.sleep(.04)
                waiting=refreshed.get('/api/agent-jobs/'+ident).json()
                self.assertEqual(waiting['progress'],45)
                self.assertEqual(waiting['updated'],observed['updated'])
            self.release.set();finished=self.wait_job(ident)
        self.assertEqual(finished['status'],'done')
        self.assertEqual(finished['progress'],100)
        current=self.client.get('/api/projects/'+self.project['id']).json()
        self.assertIsNone(current['active_agent_job'])
        self.assertEqual(current['understanding']['media'][0]['summary'],'mocked visual evidence')
        self.module.write_project(current)
        self.assertNotIn('active_agent_job',self.module.read_project(self.project['id']))
        self.assertEqual(current['timeline'],self.project['timeline'])

    def test_duplicate_and_old_synchronous_requests_share_the_project_lock(self):
        with patch.object(self.module,'analyze_project',side_effect=self.slow_analysis):
            first=self.submit();self.assertEqual(first.status_code,202)
            self.assertTrue(self.entered.wait(2))
            self.assertEqual(self.submit('edit').status_code,409)
            base='/api/projects/'+self.project['id']
            self.assertEqual(self.client.post(base+'/analyze').status_code,409)
            self.assertEqual(self.client.post(base+'/chat',json={
                'text':'Try another cut','revision':self.project['revision']}).status_code,409)
            self.release.set();self.assertEqual(self.wait_job(first.json()['id'])['status'],'done')
            # Terminal jobs do not keep the project busy.
            retry=self.submit();self.assertEqual(retry.status_code,202)
            self.assertEqual(self.wait_job(retry.json()['id'])['status'],'done')

    def test_edit_runs_visual_then_plan_and_applies_a_complete_revision(self):
        async def edit(project,text,folder,config,on_progress=None):
            self.assertIn('understanding',project)
            self.assertNotIn('file',project['media'][0])
            if on_progress:on_progress('planning',65,'Planning a grounded story')
            project['editorial_plan']={'theme':'tested story'}
            return ([{**self.project['timeline'][0],'duration':2,'caption':'new caption'}],
                    '16:9','Model planned a story')
        with patch.object(self.module,'analyze_project',side_effect=self.slow_analysis), \
             patch.object(self.module,'create_edit',side_effect=edit):
            started=self.submit('edit');self.assertEqual(started.status_code,202)
            self.release.set();job=self.wait_job(started.json()['id'])
        self.assertEqual(job['status'],'done',job)
        current=self.client.get('/api/projects/'+self.project['id']).json()
        self.assertEqual(current['revision'],2)
        self.assertEqual(job['result_revision'],current['revision'])
        self.assertEqual(current['timeline'][0]['duration'],2)
        self.assertEqual(current['timeline'][0]['caption'],'new caption')
        self.assertEqual(current['editorial_plan']['theme'],'tested story')
        self.assertEqual(current['plan_revision'],2)
        self.assertEqual(current['messages'][-2],{'role':'user','text':'Cut a short story'})
        self.assertEqual(len(self.module.read_project(current['id'])['history']),1)

    def test_failure_keeps_timeline_and_messages_and_releases_busy_state(self):
        async def fail(*args,**kwargs):raise ValueError('MODEL_UNAVAILABLE '+('x'*3000))
        before=self.module.read_project(self.project['id'])
        with patch.object(self.module,'analyze_project',side_effect=fail):
            response=self.submit('edit');self.assertEqual(response.status_code,202)
            failed=self.wait_job(response.json()['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertTrue(failed['error'].startswith('MODEL_UNAVAILABLE'))
        self.assertLessEqual(len(failed['error']),2400)
        self.assertEqual(failed['request_text'],'Cut a short story')
        self.assertEqual(self.module.read_project(self.project['id']),before)
        self.assertNotIn(self.project['id'],self.module.INTELLIGENCE_ACTIVE)
        self.assertIsNone(self.client.get('/api/projects/'+self.project['id']).json()['active_agent_job'])

    def test_user_revision_changed_while_waiting_rejects_the_old_plan(self):
        async def edit(*args,**kwargs):
            return ([{**self.project['timeline'][0],'duration':2}],'16:9','Old async proposal')
        with patch.object(self.module,'analyze_project',side_effect=self.slow_analysis), \
             patch.object(self.module,'create_edit',side_effect=edit):
            started=self.submit('edit');self.assertTrue(self.entered.wait(2))
            manual=self.client.put('/api/projects/'+self.project['id']+'/timeline',json={
                'timeline':[{**self.project['timeline'][0],'caption':'user wins'}],
                'aspect':'1:1','revision':self.project['revision']})
            self.assertEqual(manual.status_code,200,manual.text)
            self.release.set();job=self.wait_job(started.json()['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn('发生变化',job['error'])
        current=self.client.get('/api/projects/'+self.project['id']).json()
        self.assertEqual(current['timeline'],manual.json()['timeline'])
        self.assertEqual(current['aspect'],'1:1')
        self.assertNotIn('understanding',current)
        self.assertEqual(current['messages'],self.project['messages'])

    def test_material_changes_without_revision_change_also_reject_analysis(self):
        with patch.object(self.module,'analyze_project',side_effect=self.slow_analysis):
            started=self.submit();self.assertTrue(self.entered.wait(2))
            newer=self.module.read_project(self.project['id'])
            newer['media'].append({'id':'new-source','kind':'image','name':'new.jpg'})
            self.module.write_project(newer)
            self.release.set();job=self.wait_job(started.json()['id'])
        self.assertEqual(job['status'],'failed')
        current=self.module.read_project(self.project['id'])
        self.assertEqual(len(current['media']),2)
        self.assertNotIn('understanding',current)

    def test_basic_and_text_only_api_paths_keep_their_existing_behavior(self):
        with patch.object(self.module,'settings',return_value={'provider':'basic'}), \
             patch.object(self.module,'analyze_project',AsyncMock()) as visual:
            response=self.submit('edit',text='第1段改成2秒')
            job=self.wait_job(response.json()['id'])
        self.assertEqual(job['status'],'done',job)
        visual.assert_not_awaited()
        current=self.client.get('/api/projects/'+self.project['id']).json()
        self.assertEqual(current['timeline'][0]['duration'],2)
        self.project=current
        async def text_edit(project,text,config,folder):
            self.assertNotIn('understanding',project)
            return ([{**current['timeline'][0],'duration':3}],'16:9','Text-only mock plan')
        with patch.object(self.module,'settings',return_value={
            'provider':'api','api_key':'test-only','send_frames':False}), \
             patch.object(self.module,'analyze_project',AsyncMock()) as visual, \
             patch.object(self.module,'model_edit',side_effect=text_edit):
            response=self.submit('edit');job=self.wait_job(response.json()['id'])
        self.assertEqual(job['status'],'done',job)
        visual.assert_not_awaited()
        self.assertEqual(self.client.get('/api/projects/'+self.project['id']).json()['timeline'][0]['duration'],3)

    def test_validation_authentication_and_public_health(self):
        self.assertEqual(self.submit(revision=0).status_code,409)
        self.assertEqual(self.submit('edit',text='  ').status_code,400)
        self.assertEqual(self.submit('invalid').status_code,422)
        self.assertEqual(len(self.module.AGENT_JOBS),0)
        unknown=self.client.get('/api/agent-jobs/'+'a'*32)
        self.assertEqual(unknown.status_code,404)
        with TestClient(self.module.app) as stranger:
            self.assertEqual(stranger.get('/api/agent-jobs/'+'a'*32).status_code,401)
            health=stranger.get('/healthz')
            self.assertEqual(health.status_code,200)
            self.assertEqual(health.json(),{'app':'suijian-studio','version':'0.5.0'})


if __name__=='__main__':unittest.main(verbosity=2)
