"""Local FFmpeg media operations shared by Director review and assembly."""
import hashlib
import json
import math
import subprocess
from pathlib import Path


def ff(command, *arguments, timeout=120):
    result = subprocess.run([str(command), *map(str, arguments)], capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        message = result.stderr.decode('utf-8', 'replace').strip()[-700:]
        raise ValueError(f'{Path(command).stem} 失败：{message}')
    return result.stdout


def probe(encoder, path):
    encoder = Path(encoder)
    command = encoder.with_name('ffprobe' + encoder.suffix)
    metadata = json.loads(ff(command, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path))
    video = next((stream for stream in metadata['streams'] if stream['codec_type'] == 'video'), None)
    audio = next((stream for stream in metadata['streams'] if stream['codec_type'] == 'audio'), None)
    if not video:
        raise ValueError('素材没有视频轨。')
    duration = float(video.get('duration', metadata['format'].get('duration', 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('媒体时长无效。')
    return dict(duration=duration, width=video['width'], height=video['height'],
                fps=video['avg_frame_rate'], audio=bool(audio),
                audio_duration=float(audio.get('duration', metadata['format'].get('duration', 0))) if audio else None)


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def frames(encoder, path, times):
    for index, timestamp in enumerate(times, start=1):
        data = ff(encoder, '-v', 'error', '-ss', f'{timestamp:.6f}', '-i', path, '-an', '-vf',
                  'scale=448:-2', '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-')
        if not data:
            raise ValueError('无法读取指定时间的审片画面。')
        yield dict(index=index, time_sec=round(timestamp, 6)), data


def sample_times(duration):
    last = max(0, duration - .125)
    return sorted({round(last * portion, 6) for portion in (0, .25, .5, .75, 1)})
