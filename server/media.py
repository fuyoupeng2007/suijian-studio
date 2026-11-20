"""Validated, bounded FFmpeg execution. AI never produces executable commands."""
import json
import os
import shutil
import subprocess
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def binary(name):
    value = os.environ.get(name.upper() + '_PATH') or shutil.which(name)
    bundled=Path(__file__).resolve().parent.parent/'bin'/(name+'.exe' if os.name=='nt' else name)
    if not value and bundled.exists():
        value=str(bundled)
    if not value:
        raise ValueError(f'未找到 {name}，请安装 FFmpeg 或配置 {name.upper()}_PATH')
    return value


def run(args, timeout=180):
    try:
        result = subprocess.run([str(x) for x in args], capture_output=True, timeout=timeout,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'媒体处理超过 {timeout} 秒，请尝试更小的素材或较短片段') from exc
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace')[-1800:]
        raise ValueError('媒体处理失败：' + detail)
    return result.stdout


def probe(path):
    info = json.loads(run([binary('ffprobe'), '-v', 'error', '-show_streams',
                           '-show_format', '-of', 'json', path], 40))
    streams = info.get('streams', [])
    video = next((s for s in streams if s['codec_type'] == 'video'), None)
    audio = next((s for s in streams if s['codec_type'] == 'audio'), None)
    return {'width': int(video.get('width', 0)) if video else 0,
            'height': int(video.get('height', 0)) if video else 0,
            'duration': float(info.get('format', {}).get('duration', 0) or 0),
            'has_audio': bool(audio), 'has_video': bool(video),
            'codec': video.get('codec_name') if video else None}


def thumbnail(path, target, image=False, seconds=0):
    args = [binary('ffmpeg'), '-v', 'error', '-y']
    if not image:
        args += ['-ss', str(seconds)]
    run(args + ['-i', path, '-frames:v', '1', '-vf', 'scale=480:-2', target], 45)


def ass_text(value):
    return value.replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\n', r'\N').replace('\r', '')


def ass_time(seconds):
    centis = round(seconds * 100)
    return f'{centis // 360000}:{centis // 6000 % 60:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}'


def _render_timeline(timeline):
    """Allocate whole frames against the cumulative clock, without editing the plan."""
    from .effects import transition_for

    logical_end = Decimal(0)
    previous_end = 0
    previous_part = 0
    result = []
    for i, clip in enumerate(timeline):
        transition = transition_for(clip, timeline[i - 1] if i else None)
        overlap = Decimal(str(transition['duration']))
        logical_end += Decimal(str(clip['duration'])) - overlap
        target_end = int((logical_end * 30).to_integral_value(rounding=ROUND_HALF_UP))
        delta = target_end - previous_end
        incoming_frames = 0
        if transition['type'] != 'cut':
            requested = int((overlap * 30).to_integral_value(rounding=ROUND_HALF_UP))
            incoming_frames = min(requested, previous_part // 2, delta, 900 - delta)
            if incoming_frames < 3:
                raise ValueError('该转场无法在30帧时间线上安全保留，请加长相邻镜头或使用直接切换')
        part_frames = delta + incoming_frames
        if not 6 <= part_frames <= 900:
            raise ValueError('镜头无法对齐到30帧时间线，请调整镜头时长')
        rendered = {**clip, 'duration': part_frames / 30,
                    'transition': {'type': transition['type'], 'duration': incoming_frames / 30}}
        result.append(rendered)
        previous_end, previous_part = target_end, part_frames
    return result


def render(project, folder, job_id, progress, quality='preview'):
    from .planner import validate_timeline
    timeline = _render_timeline(validate_timeline(project['timeline'], project['media']))
    jobdir = folder / 'renders' / job_id
    jobdir.mkdir(parents=True, exist_ok=True)
    width, height = {'9:16': (720, 1280), '16:9': (1280, 720), '1:1': (720, 720)}[project['aspect']]
    if quality == 'preview':
        width, height = width // 2, height // 2
    indexed = {m['id']: m for m in project['media']}
    parts = []
    for i, clip in enumerate(timeline):
        progress(5 + int(i / len(timeline) * 70), f'正在剪辑第 {i + 1} 段')
        material = indexed[clip['media_id']]
        source = folder / material['file']
        args = [binary('ffmpeg'), '-v', 'error', '-y']
        if material['kind'] == 'image':
            args += ['-loop', '1']
        else:
            args += ['-ss', str(clip['start'])]
        args += ['-i', source]
        if not material['has_audio'] or material['kind'] == 'image':
            args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
            audio_map = '1:a:0'
        else:
            audio_map = '0:a:0'
        # Fit the whole original frame rather than silently cropping someone's face.
        vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
              f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x121212,setsar=1,fps=30,setpts=PTS-STARTPTS,'
              f'tpad=stop_mode=clone:stop_duration=0.1')
        look=clip.get('look','natural')
        looks={'natural':'','warm':',eq=saturation=1.04:brightness=0.01,colorbalance=rs=0.025:bs=-0.02',
               'soft':',eq=contrast=0.96:brightness=0.015:saturation=0.96','bw':',hue=s=0'}
        if look not in looks:raise ValueError('不支持的画面风格')
        vf+=looks[look]
        motion=clip.get('motion','none')
        if motion not in ('none','push_in','pull_out'):raise ValueError('不支持的画面运动')
        if motion!='none':
            if material['kind']!='image':raise ValueError('轻推拉当前仅支持照片')
            frames=max(1,round(clip['duration']*30))
            zoom=f'1+0.06*on/{frames}' if motion=='push_in' else f'1.06-0.06*on/{frames}'
            vf+=f",zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps=30"
        output = jobdir / f'part-{i:03d}.mp4'
        part_frames = round(clip['duration'] * 30)
        args += ['-map', '0:v:0', '-map', audio_map, '-t', str(clip['duration']),
                 '-frames:v', str(part_frames),
                 '-vf', vf, '-af', f'asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,volume={clip["volume"]},apad',
                 '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22', '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-b:a', '128k', '-threads', '2', '-map_metadata', '-1',
                 '-movie_timescale', '48000', output]
        run(args, 300)
        parts.append(output)
    joined = jobdir / 'joined.mp4'
    from .effects import join_parts,transition_for
    expected=join_parts(parts,timeline,joined)
    progress(82, '正在合成字幕与声音')
    font_size = 21 if quality == 'preview' else 42
    script = ('[Script Info]\nScriptType: v4.00+\n'
              f'PlayResX: {width}\nPlayResY: {height}\n'
              '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
              'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, '
              'Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
              f'Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,'
              f'0,0,0,0,100,100,0,0,1,2,1,2,20,20,{int(height * .09)},1\n'
              '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    cursor = 0.0
    has_captions = False
    for i,clip in enumerate(timeline):
        cursor-=transition_for(clip,timeline[i-1] if i else None)['duration']
        end=cursor+clip['duration']
        if i+1<len(timeline):
            end-=transition_for(timeline[i+1],clip)['duration']
        if clip['caption'].strip():
            has_captions = True
            script += f'Dialogue: 0,{ass_time(cursor)},{ass_time(end)},Default,,0,0,0,,{ass_text(clip["caption"])}\n'
        cursor += clip['duration']
    subtitle = jobdir / 'captions.ass'
    subtitle.write_text(script, encoding='utf-8-sig')
    final = jobdir / 'film.mp4'
    args = [binary('ffmpeg'), '-v', 'error', '-y', '-i', joined]
    music = indexed.get(project.get('music_id'))
    if music:
        args += ['-stream_loop', '-1', '-i', folder / music['file'], '-filter_complex',
                 f'[1:a]volume={project["music_volume"]}[bg];[0:a][bg]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]',
                 '-map', '0:v:0', '-map', '[a]']
    if has_captions:
        # FFmpeg filter paths use forward slashes and an escaped drive colon on Windows.
        subpath = subtitle.resolve().as_posix().replace(':', r'\:').replace("'", r"\'")
        args += ['-vf', f"setpts=PTS-STARTPTS,ass=filename='{subpath}'", '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22', '-threads', '2']
    else:
        # AAC encoder priming can make concat shift the first picture by ~21 ms.
        # Remove that constant while preserving B-frame PTS/DTS ordering and copy.
        args += ['-c:v', 'copy', '-bsf:v', 'setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS']
    args += ['-c:a', 'aac', '-b:a', '128k', '-t', str(expected), '-map_metadata', '-1', '-movflags', '+faststart', final]
    run(args, 300)
    verified = probe(final)
    if not verified['has_video'] or abs(verified['duration'] - expected) > .35:
        raise ValueError('输出校验失败：成片时长与时间线不一致')
    progress(100, '成片已就绪')
    # Retain only the final film and human-readable captions, avoiding redundant full-size intermediates.
    for item in parts + [joined]:
        item.unlink(missing_ok=True)
    return final.relative_to(folder).as_posix(), verified
