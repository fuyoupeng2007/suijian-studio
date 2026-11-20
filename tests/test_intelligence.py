"""Evidence and model-boundary tests; all model calls are mocked, with no requests."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image, ImageDraw

from server import intelligence, model_io


def sources():
    return [
        {'id': 'a', 'name': 'first', 'kind': 'video', 'duration': 10, 'file': 'media/a.mp4'},
        {'id': 'b', 'name': 'second', 'kind': 'video', 'duration': 20, 'file': 'media/b.mp4'},
        {'id': 'photo', 'name': 'still', 'kind': 'image', 'duration': 0, 'file': 'media/photo.png'},
        {'id': 'sound', 'name': 'music', 'kind': 'audio', 'duration': 30, 'file': 'media/music.wav'},
    ]


def panels():
    return [
        {'panel': 1, 'media_id': 'a', 'time': 0, 'frame': 'media/a-000.jpg'},
        {'panel': 2, 'media_id': 'a', 'time': 5, 'frame': 'media/a-5000.jpg'},
        {'panel': 3, 'media_id': 'b', 'time': 2.5, 'frame': 'media/b-2500.jpg'},
        {'panel': 4, 'media_id': 'photo', 'time': 0, 'frame': 'media/photo-000.jpg'},
    ]


def valid_result(evidence=None):
    evidence = evidence or panels()
    records = []
    for media in sources():
        if media['kind'] == 'audio':
            continue
        own = [p for p in evidence if p['media_id'] == media['id']]
        records.append({
            'media_id': media['id'], 'summary': 'Only the supplied samples are described.',
            'subjects': ['visible subject'], 'setting': 'location unknown',
            'events': [{'start': min(p['time'] for p in own), 'end': max(p['time'] for p in own),
                        'description': 'Visible in the cited panels.',
                        'panels': [p['panel'] for p in own], 'confidence': 'low'}],
            'camera_and_light': 'static sampled view', 'editorial_roles': ['opening candidate'],
            'uncertainties': ['The unsampled intervals and audio are unknown.'],
        })
    return {
        'media': records,
        'relations': [{'from_id': 'a', 'to_id': 'b', 'kind': 'visual_match',
                       'basis': 'Similar sampled composition; continuity remains unknown.', 'confidence': 'low'}],
        'story_outline': [{'beat': 'opening', 'media_ids': ['a', 'photo'], 'purpose': 'An optional visual introduction.'}],
        'limitations': ['Sparse visual samples cannot establish complete actions, identity, or audio content.'],
    }


class EvidenceValidation(unittest.TestCase):
    def validate(self, result):
        return intelligence.validate_understanding(result, sources(), panels())

    def test_valid_evidence_is_retained_without_inventing_audio_or_certainty(self):
        result = valid_result()
        expected = copy.deepcopy(result)
        actual = self.validate(result)
        self.assertEqual(actual, expected)
        self.assertEqual([m['media_id'] for m in actual['media']], ['a', 'b', 'photo'])
        self.assertEqual(actual['media'][0]['events'][0]['panels'], [1, 2])
        self.assertEqual(actual['relations'][0]['confidence'], 'low')
        self.assertTrue(actual['limitations'])

    def test_missing_foreign_duplicate_and_audio_media_ids_are_rejected(self):
        for operation in ('missing', 'foreign', 'duplicate', 'audio'):
            with self.subTest(operation=operation):
                result = valid_result()
                if operation == 'missing':
                    result['media'].pop()
                elif operation == 'foreign':
                    result['media'][0]['media_id'] = 'not-imported'
                elif operation == 'duplicate':
                    result['media'][1]['media_id'] = 'a'
                else:
                    result['media'].append({'media_id': 'sound'})
                with self.assertRaises(ValueError):
                    self.validate(result)

    def test_panels_must_exist_and_belong_to_the_described_source(self):
        for invalid in ([], [999], [3], [1, 3], [True], ['1']):
            with self.subTest(panels=invalid):
                result = valid_result()
                result['media'][0]['events'][0]['panels'] = invalid
                with self.assertRaises(ValueError):
                    self.validate(result)

    def test_event_times_must_be_finite_ordered_and_within_source(self):
        for start, end in ((-1, 5), (5, 4), (0, 10.1), (float('nan'), 5), (0, float('inf'))):
            with self.subTest(start=start, end=end):
                result = valid_result()
                result['media'][0]['events'][0].update(start=start, end=end)
                with self.assertRaises(ValueError):
                    self.validate(result)
        for start, end in ((0, .1), (.01, .01)):
            with self.subTest(photo_start=start, photo_end=end):
                result = valid_result()
                result['media'][2]['events'][0].update(start=start, end=end)
                with self.assertRaises(ValueError):
                    self.validate(result)

    def test_every_cited_panel_must_be_inside_event_with_seek_tolerance(self):
        for start, end in ((8, 9), (0, 4), (1, 5)):
            with self.subTest(start=start, end=end):
                result = valid_result()
                result['media'][0]['events'][0].update(start=start, end=end)
                with self.assertRaises(ValueError):
                    self.validate(result)
        result = valid_result()
        result['media'][0]['events'][0].update(start=0, end=4.97)
        self.assertEqual(self.validate(result)['media'][0]['events'][0]['panels'], [1, 2])

    def test_relations_reject_unknown_self_and_illegal_types(self):
        for change in ({'from_id': 'unknown'}, {'to_id': 'sound'}, {'to_id': 'a'},
                       {'kind': 'same_person'}, {'confidence': 'certain'}):
            with self.subTest(change=change):
                result = valid_result()
                result['relations'][0].update(change)
                with self.assertRaises(ValueError):
                    self.validate(result)
        for kind in ('same_topic', 'possible_continuation', 'detail_of', 'contrast', 'visual_match', 'uncertain'):
            with self.subTest(allowed_kind=kind):
                result = valid_result()
                result['relations'][0]['kind'] = kind
                self.assertEqual(self.validate(result)['relations'][0]['kind'], kind)

    def test_unknown_outline_source_and_bad_confidence_are_rejected(self):
        result = valid_result()
        result['story_outline'][0]['media_ids'].append('not-imported')
        with self.assertRaises(ValueError):
            self.validate(result)
        result = valid_result()
        result['media'][0]['events'][0]['confidence'] = 'certain'
        with self.assertRaises(ValueError):
            self.validate(result)

    def test_malformed_api_objects_are_rejected_as_validation_errors(self):
        for field, invalid in (('media', [None]), ('relations', [None]), ('relations', {}),
                               ('story_outline', [None]), ('limitations', None)):
            with self.subTest(field=field, invalid=invalid):
                result = valid_result()
                result[field] = invalid
                with self.assertRaises(ValueError):
                    self.validate(result)
        for invalid in (None, {}, [None], [{'start': None, 'end': 1, 'panels': [1]}]):
            with self.subTest(events=invalid):
                result = valid_result()
                result['media'][0]['events'] = invalid
                with self.assertRaises(ValueError):
                    self.validate(result)


class ProjectFixtures:
    def make_files(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / 'media').mkdir()
        self.project = {'media': sources(), 'timeline': [], 'aspect': '16:9'}
        self.reports = {}
        for item in sources():
            (self.folder / item['file']).write_bytes(b'source fixture; decoding is mocked')
            if item['kind'] == 'audio':
                continue
            samples = []
            for p in [p for p in panels() if p['media_id'] == item['id']]:
                color = {'a': 'red', 'b': 'blue', 'photo': 'green'}[item['id']]
                Image.new('RGB', (80, 60), color).save(self.folder / p['frame'])
                samples.append({'time': p['time'], 'frame': p['frame'], 'brightness': .4})
            self.reports[item['id']] = {
                'samples': samples, 'scenes': [],
                'coverage': {'duration': item['duration'], 'sample_count': len(samples),
                             'max_gap': item['duration'], 'semantic_understanding': False},
                'limitations': ['Sparse samples; gaps and audio have not been understood.'],
            }
        self.config = {'provider': 'codex', 'model': 'mock-vision'}

    def cv_report(self, item, folder):
        return copy.deepcopy(self.reports[item['id']])


class ContactSheets(ProjectFixtures, unittest.TestCase):
    def setUp(self):
        self.make_files()

    def test_multiple_sources_keep_true_sample_seconds_and_distinct_panels(self):
        run_dir = self.folder / 'run'
        run_dir.mkdir()
        labels = []
        original_text = ImageDraw.ImageDraw.text

        def record_text(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original_text(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, 'text', record_text):
            images, evidence, coverage = intelligence.sheets(self.project, self.reports, self.folder, run_dir)
        self.assertEqual(len(images), 3)
        self.assertEqual(evidence, panels())
        self.assertIn('P2 / S1 / 5.00s', labels)
        self.assertIn('P3 / S2 / 2.50s', labels)
        self.assertNotIn('5000.00s', ' '.join(labels))
        self.assertEqual([c['media_id'] for c in coverage], ['a', 'b', 'photo'])
        self.assertEqual([c['model_max_gap'] for c in coverage], [5, 17.5, 0])
        self.assertTrue(all(c['cv']['semantic_understanding'] is False for c in coverage))
        self.assertTrue(all(c['limitations'] for c in coverage))
        for image, color in zip(images, ((255, 0, 0), (0, 0, 255), (0, 128, 0))):
            with Image.open(image) as sheet:
                pixel = sheet.getpixel((150, 120))
                self.assertLess(sum(abs(a - b) for a, b in zip(pixel, color)), 12)

    def test_sheet_budget_updates_model_coverage_after_downsampling(self):
        project = {'media': []}
        reports = {}
        for index in range(20):
            ident = f'v{index}'
            project['media'].append({'id': ident, 'kind': 'video', 'duration': 100})
            report = copy.deepcopy(self.reports['a'])
            report['samples'] = [{'time': i * 4, 'frame': panels()[0]['frame']} for i in range(24)]
            reports[ident] = report
        run_dir = self.folder / 'many'
        run_dir.mkdir()
        images, evidence, coverage = intelligence.sheets(project, reports, self.folder, run_dir)
        self.assertEqual(len(images), 20)
        self.assertLessEqual(len(evidence), 160)
        self.assertEqual(len({p['panel'] for p in evidence}), len(evidence))
        for item in coverage:
            own = [p for p in evidence if p['media_id'] == item['media_id']]
            self.assertEqual(item['model_sample_count'], len(own))
            self.assertEqual(own[0]['time'], 0)
            self.assertEqual(own[-1]['time'], 92)
            times = [0, *[p['time'] for p in own], 100]
            self.assertEqual(item['model_max_gap'], max(b - a for a, b in zip(times, times[1:])))


class ProjectAnalysis(ProjectFixtures, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_files()

    async def test_analysis_preserves_evidence_coverage_and_reuses_valid_cache(self):
        model = AsyncMock(side_effect=lambda *args: valid_result())
        with patch.object(intelligence, 'analyze_media', side_effect=self.cv_report) as cv, \
             patch.object(intelligence, 'call_json', model):
            result = await intelligence.analyze_project(self.project, self.folder, self.config)
            cached = await intelligence.analyze_project(self.project, self.folder, self.config)
        self.assertEqual(model.await_count, 1)
        self.assertEqual(cv.call_count, 3)
        self.assertEqual(cached, result)
        self.assertEqual(result['evidence'], panels())
        self.assertEqual(len(result['coverage']), 3)
        self.assertTrue(all(c['limitations'] for c in result['coverage']))
        prompt = model.call_args.args[1]
        payload = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(payload['evidence'], panels())
        self.assertEqual(payload['coverage'], result['coverage'])
        self.assertIn('未分析音频', prompt)
        self.assertFalse(any(m['id'] == 'sound' for m in payload['media']))
        self.assertNotIn('api_key', json.dumps(result))

    async def test_changed_source_or_model_invalidates_semantic_cache(self):
        model = AsyncMock(side_effect=lambda *args: valid_result())
        with patch.object(intelligence, 'analyze_media', side_effect=self.cv_report), \
             patch.object(intelligence, 'call_json', model):
            first = await intelligence.analyze_project(self.project, self.folder, self.config)
            (self.folder / 'media/a.mp4').write_bytes(b'changed source with different length')
            second = await intelligence.analyze_project(self.project, self.folder, self.config)
            third = await intelligence.analyze_project(self.project, self.folder, {**self.config, 'model': 'another-model'})
        self.assertEqual(model.await_count, 3)
        self.assertNotEqual(first['signature'], second['signature'])
        self.assertNotEqual(second['signature'], third['signature'])

    async def test_changing_model_service_invalidates_cache_even_for_the_same_model_name(self):
        model=AsyncMock(side_effect=lambda *args:valid_result())
        with patch.object(intelligence,'analyze_media',side_effect=self.cv_report), \
             patch.object(intelligence,'call_json',model):
            first=await intelligence.analyze_project(self.project,self.folder,{**self.config,'base_url':'https://one.invalid/v1'})
            second=await intelligence.analyze_project(self.project,self.folder,{**self.config,'base_url':'https://two.invalid/v1'})
        self.assertEqual(model.await_count,2)
        self.assertNotEqual(first['signature'],second['signature'])

    async def test_same_size_source_replacement_with_preserved_mtime_invalidates_cache(self):
        model=AsyncMock(side_effect=lambda *args:valid_result())
        path=self.folder/'media/a.mp4';before=path.stat()
        with patch.object(intelligence,'analyze_media',side_effect=self.cv_report), \
             patch.object(intelligence,'call_json',model):
            first=await intelligence.analyze_project(self.project,self.folder,self.config)
            path.write_bytes(b'x'*before.st_size)
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            self.assertEqual(path.stat().st_size,before.st_size)
            self.assertEqual(path.stat().st_mtime_ns,before.st_mtime_ns)
            second=await intelligence.analyze_project(self.project,self.folder,self.config)
        self.assertEqual(model.await_count,2)
        self.assertNotEqual(first['signature'],second['signature'])

    async def test_corrupt_cache_or_missing_evidence_is_rebuilt(self):
        model=AsyncMock(side_effect=lambda *args:valid_result())
        original_cv=self.cv_report
        def rebuild(item,folder):
            report=original_cv(item,folder)
            for sample in report['samples']:
                Image.new('RGB',(80,60),'red').save(folder/sample['frame'])
            return report
        with patch.object(intelligence,'analyze_media',side_effect=rebuild), \
             patch.object(intelligence,'call_json',model):
            await intelligence.analyze_project(self.project,self.folder,self.config)
            (self.folder/'understanding.json').write_text('{incomplete',encoding='utf8')
            await intelligence.analyze_project(self.project,self.folder,self.config)
            (self.folder/panels()[0]['frame']).unlink()
            final=await intelligence.analyze_project(self.project,self.folder,self.config)
        self.assertEqual(model.await_count,3)
        self.assertEqual(final['evidence'],panels())
        self.assertTrue((self.folder/panels()[0]['frame']).is_file())

    async def test_invalid_model_evidence_is_not_saved_and_project_is_unchanged(self):
        before = copy.deepcopy(self.project)
        invalid = valid_result()
        invalid['media'][0]['events'][0]['panels'] = [3]
        with patch.object(intelligence, 'analyze_media', side_effect=self.cv_report), \
             patch.object(intelligence, 'call_json', AsyncMock(return_value=invalid)):
            with self.assertRaises(ValueError):
                await intelligence.analyze_project(self.project, self.folder, self.config)
        self.assertFalse((self.folder / 'understanding.json').exists())
        self.assertEqual(self.project, before)

    async def test_permission_and_media_limits_fail_before_model_or_cv(self):
        cases = [({'provider': 'api', 'api_key': 'test-only', 'send_frames': False}, self.project),
                 (self.config, {'media': [sources()[3]]}),
                 (self.config, {'media': [sources()[0]] * 21})]
        with patch.object(intelligence, 'analyze_media') as cv, \
             patch.object(intelligence, 'call_json', AsyncMock()) as model:
            for config, project in cases:
                with self.subTest(config=config, count=len(project['media'])):
                    with self.assertRaises(ValueError):
                        await intelligence.analyze_project(project, self.folder, config)
        cv.assert_not_called()
        model.assert_not_awaited()

    async def test_editor_receives_sampling_coverage_and_evidence_not_summary_alone(self):
        self.project['understanding'] = valid_result()
        self.project['understanding'].update(evidence=panels(), coverage=[
            {'media_id': 'a', 'duration': 10, 'model_sample_count': 2, 'model_max_gap': 5,
             'cv': {'semantic_understanding': False, 'sample_count': 2, 'max_gap': 5},
             'limitations': ['Unsampled intervals are unknown.']}
        ])
        answer = {'timeline': [{'media_id': 'a', 'start': 0, 'duration': 3, 'volume': 1}],
                  'aspect': '16:9', 'message': 'A sampled opening candidate.',
                  'editorial_plan': {'theme': 'sampled views', 'structure': ['opening'], 'pace': 'slow',
                                     'style': 'natural', 'transition_strategy': 'cut',
                                     'audio_strategy': 'not analyzed', 'uncertainties': ['Unseen intervals.']}}
        model = AsyncMock(return_value=answer)
        with patch.object(intelligence, 'call_json', model):
            await intelligence.create_edit(self.project, 'Give me an opening.', self.folder, self.config)
        prompt = model.call_args.args[1]
        payload = json.loads(prompt.split('\n', 1)[1])
        understanding = payload['understanding']
        self.assertTrue(understanding.get('coverage'), 'The planning model needs actual sampling gaps.')
        self.assertTrue(understanding.get('evidence'), 'Source panels and timestamps must survive summarization.')
        self.assertIn('model_max_gap', json.dumps(understanding))
        self.assertIn('semantic_understanding', json.dumps(understanding))
        self.assertTrue(all('frame' not in p for p in understanding['evidence']))
        self.assertIn('不把采样信息当完整视频理解', prompt)


class ModelBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_json_object_and_markdown_are_supported_but_arrays_are_rejected(self):
        self.assertEqual(model_io.decode_json('{"ok": true}'), {'ok': True})
        self.assertEqual(model_io.decode_json('```json\n{"ok": true}\n```'), {'ok': True})
        for raw in ('[]', 'null', 'not json'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                model_io.decode_json(raw)

    async def test_api_sends_actual_images_and_saves_only_parsed_object(self):
        images = []
        for i, color in enumerate(('red', 'blue')):
            path = self.folder / f'{i}.jpg'
            Image.new('RGB', (8, 8), color).save(path)
            images.append(path)
        response = MagicMock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': '{"ok": true}'}}]}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)
        config = {'provider': 'api', 'api_key': 'test-secret', 'base_url': 'https://invalid.example/v1/', 'model': 'mock'}
        with patch.object(model_io.httpx, 'AsyncClient', return_value=client):
            result = await model_io.call_json(config, 'Use only samples.', {'type': 'object'}, images, self.folder / 'run')
        self.assertEqual(result, {'ok': True})
        sent = client.post.call_args.kwargs['json']['messages'][0]['content']
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(part['image_url']['url'].startswith('data:image/jpeg;base64,') for part in sent[1:]))
        self.assertNotEqual(sent[1]['image_url']['url'], sent[2]['image_url']['url'])
        saved = (self.folder / 'run/result.json').read_text(encoding='utf8')
        self.assertEqual(json.loads(saved), result)
        self.assertNotIn('test-secret', saved)

    async def test_bad_api_content_or_http_failure_is_a_validation_error(self):
        config = {'provider': 'api', 'api_key': 'test-only', 'base_url': 'https://invalid.example/v1', 'model': 'mock'}
        cases = [(503, {}), (200, {'choices': []}),
                 (200, {'choices': [{'message': {'content': '[]'}}]}),
                 (200, {'choices': [{'message': {'content': None}}]})]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                response = MagicMock(status_code=status)
                response.json.return_value = body
                client = MagicMock()
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                client.post = AsyncMock(return_value=response)
                run_dir = self.folder / f'failure-{len(list(self.folder.iterdir()))}'
                with patch.object(model_io.httpx, 'AsyncClient', return_value=client), self.assertRaises(ValueError):
                    await model_io.call_json(config, 'prompt', {}, [], run_dir)
                self.assertFalse((run_dir / 'result.json').exists())

    def test_codex_is_bounded_read_only_and_timeout_is_reported(self):
        run_dir = self.folder / 'cli'

        def fake_run(command, **kwargs):
            (run_dir / 'result.json').write_text('{"ok": true}', encoding='utf8')
            self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
            self.assertLessEqual(kwargs['timeout'], 300)
            self.assertEqual(kwargs['input'], b'only supplied evidence')
            return subprocess.CompletedProcess(command, 0)

        with patch.object(model_io.shutil, 'which', return_value='mock-codex'), \
             patch.object(model_io.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(model_io.codex_json('only supplied evidence', {}, [], run_dir), {'ok': True})
        with patch.object(model_io.shutil, 'which', return_value='mock-codex'), \
             patch.object(model_io.subprocess, 'run', side_effect=subprocess.TimeoutExpired('mock', 300)):
            with self.assertRaises(ValueError):
                model_io.codex_json('only supplied evidence', {}, [], self.folder / 'timeout')


if __name__ == '__main__':
    unittest.main()
