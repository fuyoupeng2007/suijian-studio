"""Bounded, local low-level CV evidence; it never invents people, actions or a story.

The temporal scan samples the whole timeline at up to 2 fps / 600 tiny frames.
Those samples find visual-change candidates, not guaranteed semantic scene cuts.
At most 24 larger JPEGs are selected for a later vision-language-model pass.
"""
import copy
import hashlib
import json
import math
import statistics
import time
import uuid
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .media import binary, run, thumbnail


VERSION = 'bounded-cv-1'
MAX_VIDEO_FRAMES = 24
MAX_SCAN_FRAMES = 600
SCAN_SIZE = (64, 36)
MAX_DURATION = 3600.0
TOTAL_BUDGET_SECONDS = 90.0
SCAN_BUDGET_SECONDS = 35.0
FRAME_BUDGET_SECONDS = 10.0
LIMITATIONS = [
    '这是有时间与帧数上限的视觉采样，不能保证发现所有短暂事件或完整动作。',
    '场景边界是低分辨率像素变化候选，镜头运动、闪光和曝光变化也可能产生边界。',
    '亮度、边缘清晰度与颜色是可测信号，不等同于审美分数、人物身份或语义判断。',
    '本模块不识别对白、音乐语义、人物身份或素材之间的故事关系；需后续模型结合证据判断。',
    '采样时间是请求截帧的位置，实际画面时间受源帧率与解码寻帧精度影响。',
]


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ValueError('本地视觉分析达到90秒上限，请使用更短或更小的素材')
    return value


def _fingerprint(source, item, deadline):
    stat = source.stat()
    if stat.st_size > 500 * 1024 * 1024:
        raise ValueError('本地视觉分析仅支持不超过500 MB的素材')
    digest = hashlib.sha256()
    with source.open('rb') as file:
        while chunk := file.read(1024 * 1024):
            _remaining(deadline)
            digest.update(chunk)
    return {
        'sha256': digest.hexdigest(), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
        'kind': item['kind'], 'duration': float(item.get('duration', 0) or 0),
        'version': VERSION,
    }


def _normal_image(image):
    return image.convert('RGB').resize(SCAN_SIZE, Image.Resampling.BILINEAR)


def _change(before, after):
    if before is None:
        return None
    return round(sum(ImageStat.Stat(ImageChops.difference(before, after)).mean) / (3 * 255), 4)


def _measure(image):
    rgb = image.convert('RGB')
    # Use a fixed image size so the numerical metrics are comparable across samples.
    rgb.thumbnail((480, 480))
    gray = rgb.convert('L')
    stat = ImageStat.Stat(gray)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    if edges.width > 4 and edges.height > 4:
        edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))
    return {
        'brightness': round(stat.mean[0] / 255, 4),
        'contrast': round(stat.stddev[0] / 255, 4),
        'sharpness': round(ImageStat.Stat(edges).var[0], 3),
        'mean_rgb': [round(n) for n in ImageStat.Stat(rgb).mean],
    }


def _scan_video(source, duration, deadline):
    rate = min(2.0, MAX_SCAN_FRAMES / duration)
    interval = 1 / rate
    args = [binary('ffmpeg'), '-v', 'error', '-nostdin', '-i', source,
            '-an', '-sn', '-vf', f'fps={rate:.8f},scale=64:36:flags=fast_bilinear',
            '-frames:v', str(MAX_SCAN_FRAMES), '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']
    raw = run(args, timeout=min(SCAN_BUDGET_SECONDS, _remaining(deadline)))
    frame_bytes = SCAN_SIZE[0] * SCAN_SIZE[1] * 3
    count = min(MAX_SCAN_FRAMES, len(raw) // frame_bytes)
    frames = []
    previous = None
    for i in range(count):
        _remaining(deadline)
        image = Image.frombytes('RGB', SCAN_SIZE, raw[i * frame_bytes:(i + 1) * frame_bytes])
        frames.append({'time': round(min(duration, i * interval), 3),
                       'change': _change(previous, image) or 0.0})
        previous = image
    complete = bool(count) and count * interval >= duration - interval * 1.1
    return frames, interval, complete


def _scene_candidates(scan, duration, interval):
    changes = [f['change'] for f in scan[1:]]
    median = statistics.median(changes) if changes else 0
    mad = statistics.median(abs(n - median) for n in changes) if changes else 0
    threshold = max(.16, median + 3 * mad)
    minimum_gap = max(.5, interval)
    candidates = sorted((f for f in scan[1:] if f['change'] >= threshold),
                        key=lambda f: f['change'], reverse=True)
    cuts = []
    for frame in candidates:
        at = frame['time']
        if at < minimum_gap or duration - at < minimum_gap:
            continue
        if any(abs(at - f['time']) < minimum_gap for f in cuts):
            continue
        cuts.append(frame)
        if len(cuts) >= MAX_VIDEO_FRAMES - 1:
            break
    cuts.sort(key=lambda f: f['time'])
    boundaries = [0.0] + [f['time'] for f in cuts] + [duration]
    scenes = []
    for i, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        if end > start:
            scenes.append({'index': i, 'start': round(start, 3), 'end': round(end, 3),
                           'change_at_start': cuts[i - 1]['change'] if i else 0.0})
    return scenes


def sample_times(duration, scenes):
    """Cover endpoints and each candidate scene, then subdivide the largest gaps."""
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION:
        raise ValueError('视频时长需大于0且不超过3600秒')
    end = max(0.0, duration - min(.25, duration / 2))
    count = min(MAX_VIDEO_FRAMES, max(4, math.ceil(duration / 3), len(scenes) * 2))
    selected = {0.0, round(end, 3)}
    # Endpoints already represent the first and last candidate scenes.
    for scene in sorted(scenes[1:-1], key=lambda s: s['end'] - s['start'], reverse=True):
        if len(selected) >= count:
            break
        selected.add(round(min(end, (scene['start'] + scene['end']) / 2), 3))
    for scene in sorted(scenes, key=lambda s: s['end'] - s['start'], reverse=True):
        if len(selected) >= count:
            break
        middle = round(min(end, (scene['start'] + scene['end']) / 2), 3)
        if all(abs(middle - at) >= .04 for at in selected):
            selected.add(middle)
    while len(selected) < count:
        ordered = sorted(selected)
        gaps = sorted(zip(ordered, ordered[1:]), key=lambda pair: pair[1] - pair[0], reverse=True)
        if not gaps or gaps[0][1] - gaps[0][0] < .08:
            break
        left, right = gaps[0]
        middle = round((left + right) / 2, 3)
        if middle in selected:
            break
        selected.add(middle)
    return sorted(selected)[:MAX_VIDEO_FRAMES]


def _extract(source, target, at, deadline):
    # Some files end with audio after the last picture, or have very low frame rates.
    # Move backwards a bounded amount if the requested tail position has no next frame.
    for offset in (0, .25, .5, 1.0):
        position = round(max(0, at - offset), 3)
        args = [binary('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-ss', str(position), '-i', source,
                '-frames:v', '1', '-vf', 'scale=480:-2', '-pix_fmt', 'yuvj420p', '-q:v', '3', target]
        run(args, timeout=min(FRAME_BUDGET_SECONDS, _remaining(deadline)))
        if target.exists() and target.stat().st_size:
            return position
    raise ValueError(f'无法在视频 {at:.2f} 秒位置附近提取画面')


def _cache_read(path, fingerprint, folder):
    try:
        data = json.loads(path.read_text(encoding='utf8'))
        if data['fingerprint'] != fingerprint:
            return None
        result = data['result']
        media_root = (folder / 'media').resolve()
        for relative in result['frames']:
            frame = (folder / relative).resolve()
            if not frame.is_relative_to(media_root) or not frame.is_file() or frame.stat().st_size == 0:
                return None
        result['cache_hit'] = True
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def analyze_media(item: dict, folder: Path) -> dict:
    """Return measurable local CV evidence and JPEGs compatible with /files/media/... ."""
    start = time.monotonic()
    deadline = start + TOTAL_BUDGET_SECONDS
    folder = Path(folder).resolve()
    kind = item.get('kind')
    if kind == 'audio':
        return {'version': VERSION, 'kind': 'audio', 'status': 'skipped', 'frames': [],
                'samples': [], 'scenes': [], 'coverage': {'duration': float(item.get('duration', 0) or 0),
                'sample_count': 0, 'max_gap': 0, 'scan_complete': False},
                'limitations': ['音频不进入视觉采样；本模块没有语音识别能力。'], 'cache_hit': False}
    if kind not in ('video', 'image'):
        raise ValueError('视觉分析只支持照片、视频或跳过音频')
    source = (folder / str(item.get('file', ''))).resolve()
    if not source.is_relative_to(folder / 'media') or not source.is_file():
        raise ValueError('素材路径必须位于项目的media目录')
    duration = float(item.get('duration', 0) or 0)
    if kind == 'video' and (not math.isfinite(duration) or not 0 < duration <= MAX_DURATION):
        raise ValueError('视频时长需大于0且不超过3600秒')
    fingerprint = _fingerprint(source, item, deadline)
    ident = hashlib.sha256(str(item.get('id', source.name)).encode()).hexdigest()[:12]
    cache_dir = folder / 'analysis'
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / (ident + '-cv.json')
    cached = _cache_read(cache_file, fingerprint, folder)
    if cached is not None:
        return cached
    content_key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:10]
    frames = []
    samples = []
    notes = copy.deepcopy(LIMITATIONS)
    scan, scan_interval, scan_complete = [], None, False
    scenes = []
    if kind == 'image':
        target = folder / 'media' / f'cv-{ident}-{content_key}-at-0.jpg'
        if _remaining(deadline) < 45:
            raise ValueError('本地视觉分析预算不足，未启动照片解码')
        thumbnail(source, target, image=True)
        _remaining(deadline)
        times = [0.0]
    else:
        try:
            scan, scan_interval, scan_complete = _scan_video(source, duration, deadline)
        except ValueError:
            # An explicitly reported fallback, never label it as a successful temporal scan.
            notes.append('低分辨率时间扫描未完成，已退回有界均匀采样，场景候选不可靠。')
        scenes = _scene_candidates(scan, duration, scan_interval or .5)
        times = sample_times(duration, scenes)
    previous = None
    for at in times:
        _remaining(deadline)
        if kind == 'video':
            target = folder / 'media' / f'cv-{ident}-{content_key}-at-{round(at * 1000)}.jpg'
            at = _extract(source, target, at, deadline)
        with Image.open(target) as image:
            metrics = _measure(image)
            normalized = _normal_image(image)
        relative = target.relative_to(folder).as_posix()
        frames.append(relative)
        samples.append({'time': at, 'frame': relative, **metrics,
                        'visual_change': _change(previous, normalized)})
        previous = normalized
    _remaining(deadline)
    now = source.stat()
    if now.st_size != fingerprint['size'] or now.st_mtime_ns != fingerprint['mtime_ns']:
        raise ValueError('分析期间源素材发生改变，请重新分析')
    ordered = [0.0] + [s['time'] for s in samples] + ([duration] if kind == 'video' else [0.0])
    max_gap = max((right - left for left, right in zip(ordered, ordered[1:])), default=0)
    result = {'version': VERSION, 'kind': kind, 'status': 'analyzed', 'frames': frames,
              'samples': samples, 'scenes': scenes, 'coverage': {
                  'duration': duration if kind == 'video' else 0,
                  'sample_count': len(samples), 'max_gap': round(max_gap, 3),
                  'scan_frame_count': len(scan),
                  'scan_interval_seconds': round(scan_interval, 3) if scan_interval else None,
                  'scan_complete': scan_complete if kind == 'video' else True,
                  'max_frames': MAX_VIDEO_FRAMES if kind == 'video' else 1,
                  'time_budget_seconds': TOTAL_BUDGET_SECONDS,
                  'elapsed_seconds': round(time.monotonic() - start, 3),
                  'semantic_understanding': False},
              'limitations': notes, 'cache_hit': False}
    temp = cache_dir / (uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps({'fingerprint': fingerprint, 'result': result}, ensure_ascii=False), encoding='utf8')
    temp.replace(cache_file)
    return result
