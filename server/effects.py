"""Small, validated transition vocabulary for normalized local media.

Every transition belongs to the incoming shot.  The first shot therefore has
no incoming transition.  No model-supplied FFmpeg expression is ever executed.
"""
import math
from pathlib import Path


TRANSITIONS = frozenset({
    'cut', 'dissolve', 'fadeblack', 'wipeleft', 'wiperight',
    'slideleft', 'slideright', 'circleopen',
})


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name}必须是数字')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f'{name}必须是有限数字')
    return value


def _duration(clip):
    if not isinstance(clip, dict):
        raise ValueError('每个镜头必须是结构化对象')
    duration = _number(clip.get('duration'), '镜头时长')
    if not .2 <= duration <= 30:
        raise ValueError('单个镜头时长必须在 0.2—30 秒之间')
    return duration


def transition_for(clip, previous=None):
    """Return a canonical incoming transition, rejecting unsafe parameters."""
    if not isinstance(clip, dict):
        raise ValueError('每个镜头必须是结构化对象')
    if previous is None:
        return {'type': 'cut', 'duration': 0.0}
    current_duration, previous_duration = _duration(clip), _duration(previous)
    raw = clip.get('transition', {'type': 'cut'})
    if not isinstance(raw, dict):
        raise ValueError('转场必须是包含 type 和 duration 的结构化对象')
    kind = raw.get('type', 'cut')
    if not isinstance(kind, str) or kind not in TRANSITIONS:
        raise ValueError('不支持该转场类型，请选择允许的转场')
    duration = _number(raw.get('duration', 0 if kind == 'cut' else .4), '转场时长')
    if kind == 'cut':
        if not 0 <= duration <= 1.2:
            raise ValueError('切换的时长参数无效')
        return {'type': 'cut', 'duration': 0.0}
    if not .1 <= duration <= 1.2:
        raise ValueError('转场时长必须在 0.1—1.2 秒之间')
    if duration > min(current_duration, previous_duration) / 2 + 1e-9:
        raise ValueError('转场不能超过前后相邻镜头各自时长的一半')
    return {'type': kind, 'duration': duration}


def timeline_duration(clips):
    """Duration after incoming transitions overlap adjacent shots."""
    if not isinstance(clips, list) or not 1 <= len(clips) <= 60:
        raise ValueError('时间线需要 1—60 个镜头')
    total = 0.0
    for index, clip in enumerate(clips):
        total += _duration(clip)
        total -= transition_for(clip, clips[index - 1] if index else None)['duration']
    return round(total, 6)


def _decimal(number):
    return f'{number:.6f}'.rstrip('0').rstrip('.') or '0'


def _file_line(path):
    # concat-demuxer quoting: close the quoted string before an escaped quote.
    return "file '" + path.as_posix().replace("'", "'\\''") + "'"


def _path(value):
    try:
        return Path(value).resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError('本地媒体路径无效') from exc


def join_parts(parts: list[Path], clips: list[dict], output: Path) -> float:
    """Join validated, equal-size 30fps/48k stereo clips and return planned length.

    Inputs are the generated normalized files, not user-supplied network URLs.
    An all-cut sequence stream-copies; sequences with transitions use a bounded
    filter graph and re-encode once.  The FFmpeg result is probed before return.
    """
    # Local import lets media.render call this helper without an import cycle.
    from .media import binary, probe, run

    expected = timeline_duration(clips)
    if not isinstance(parts, list) or len(parts) != len(clips):
        raise ValueError('标准化片段数量与时间线不一致')
    output = _path(output)
    normalized = []
    for part in parts:
        path = _path(part)
        if any(c in str(path) for c in ('\n', '\r', '\0')) or not path.is_file():
            raise ValueError('标准化视频文件不存在或路径无效')
        if path == output:
            raise ValueError('合成输出不能覆盖输入素材')
        normalized.append(path)
    if output.exists():
        raise ValueError('合成输出已存在，请使用新的输出文件名')
    output.parent.mkdir(parents=True, exist_ok=True)
    transitions = [transition_for(c, clips[i - 1] if i else None)
                   for i, c in enumerate(clips)]
    command = [binary('ffmpeg'), '-v', 'error', '-nostdin', '-y']
    if all(t['type'] == 'cut' for t in transitions):
        manifest = output.with_suffix('.concat.txt')
        manifest.write_text('\n'.join(_file_line(p) for p in normalized) + '\n', encoding='utf8')
        command += ['-f', 'concat', '-safe', '0', '-i', manifest, '-c', 'copy',
                    '-movflags', '+faststart', '-map_metadata', '-1', output]
    else:
        graph = []
        for i, (part, clip) in enumerate(zip(normalized, clips)):
            command += ['-i', part]
            length = _decimal(_duration(clip))
            graph += [f'[{i}:v:0]trim=duration={length},setpts=PTS-STARTPTS,'
                      f'fps=30,setsar=1,format=yuv420p,settb=AVTB[v{i}]',
                      f'[{i}:a:0]atrim=duration={length},asetpts=PTS-STARTPTS,'
                      f'aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}]']
        current_v, current_a = 'v0', 'a0'
        elapsed = _duration(clips[0])
        for i in range(1, len(clips)):
            transition = transitions[i]
            if transition['type'] == 'cut':
                graph += [f'[{current_v}][v{i}]concat=n=2:v=1:a=0[jv{i}]',
                          f'[{current_a}][a{i}]concat=n=2:v=0:a=1[ja{i}]']
            else:
                overlap = transition['duration']
                # A conventional dissolve is an opacity crossfade, not FFmpeg's
                # randomly distributed "dissolve" pixel replacement effect.
                effect = 'fade' if transition['type'] == 'dissolve' else transition['type']
                graph += [f'[{current_v}][v{i}]xfade=transition={effect}:'
                          f'duration={_decimal(overlap)}:offset={_decimal(elapsed - overlap)}[jv{i}]',
                          f'[{current_a}][a{i}]acrossfade=d={_decimal(overlap)}:'
                          f'c1=tri:c2=tri[ja{i}]']
            # concat and xfade can negotiate different time bases / sample formats.
            graph += [f'[jv{i}]fps=30,setsar=1,format=yuv420p,settb=AVTB[ov{i}]',
                      f'[ja{i}]aformat=sample_fmts=fltp:sample_rates=48000:'
                      f'channel_layouts=stereo[oa{i}]']
            current_v, current_a = f'ov{i}', f'oa{i}'
            elapsed += _duration(clips[i]) - transition['duration']
        graph_file = output.with_suffix('.filters.txt')
        graph_file.write_text(';\n'.join(graph), encoding='utf8')
        command += ['-filter_complex_threads', '2', '-/filter_complex', graph_file,
                    '-map', f'[{current_v}]', '-map', f'[{current_a}]',
                    '-t', _decimal(expected), '-c:v', 'libx264', '-preset', 'veryfast',
                    '-crf', '22', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                    '-threads', '2', '-movflags', '+faststart', '-map_metadata', '-1', output]
    try:
        run(command, 300)
        result = probe(output)
        if not result['has_video'] or not result['has_audio'] or abs(result['duration'] - expected) > .2:
            raise ValueError('合成校验失败：画面、音轨或转场后时长不正确')
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return expected
