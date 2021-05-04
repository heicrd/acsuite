"""Frame-based cutting/trimming/splicing of audio with VapourSynth and FFmpeg."""
__all__ = ["FFmpeg", "FFmpegAudio", "StreamType", "CompressionType", "Codec", "AudioStream",
           "clip_to_timecodes", "eztrim", "frames_to_timecodes", "get_timecodes"]

from ._metadata import __author__, __credits__, __date__, __version__  # noqa: F401
from .ffmpeg import FFmpeg, FFmpegAudio, StreamType, CompressionType, Codec, AudioStream
from .timecode import clip_to_timecodes, get_timecodes, frames_to_timecodes
from .trim import eztrim
from .log import logger  # noqa: F401
