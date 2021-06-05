import json
import os
import string
import tempfile

from enum import Enum
from shutil import which
from subprocess import CalledProcessError, run
from typing import Any, Dict, List, Literal, Optional, NamedTuple, Tuple, Union

from .log import logger


FFMPEG_CODEC_HEADER_LEN: int = 10


class StreamType(Enum):
    VIDEO: Literal["V"] = "V"
    AUDIO: Literal["A"] = "A"
    SUBTITLE: Literal["S"] = "S"
    DATA: Literal["D"] = "D"


class CompressionType(Enum):
    LOSSLESS = 0
    LOSSY = 1
    EITHER = 2
    NONE = 3


class Codec(NamedTuple):
    stream_type: StreamType
    compression_type: CompressionType
    name: str
    can_decode: bool
    can_encode: bool


class AudioStream(NamedTuple):
    codec: Optional[Codec]
    depth: Optional[int]
    stream_index: int


def get_temp_filename(prefix: str = "", suffix: str = "") -> str:
    return f"{prefix}{next(tempfile._get_candidate_names())}{suffix}"  # type: ignore


class FFmpeg():
    codecs: Dict[str, Codec]

    def __init__(self, search_path: Optional[str] = None) -> None:
        self.ffargs = ["-hide_banner", "-loglevel", "panic"]
        self.find_ffmpeg(search_path)
        self.codecs = {}
        self.get_codecs()

    def get_codecs(self) -> None:
        """
        Query ffmpeg for supported codecs.
        """
        ffout = self.ffmpeg("-codecs")
        for codec in ffout[FFMPEG_CODEC_HEADER_LEN:]:
            features = codec.split(" ")[1]
            name = codec.split(" ")[2]
            compression = CompressionType.NONE if features[4] == "." and features[5] == "." \
                else CompressionType.EITHER if features[4] == "L" and features[5] == "S" \
                else CompressionType.LOSSY if features[4] == "L" \
                else CompressionType.LOSSLESS
            self.codecs[name] = Codec(name=name,
                                      can_decode=features[0] == "D",
                                      can_encode=features[1] == "E",
                                      stream_type=StreamType(features[2]),
                                      compression_type=compression)

    def ffmpeg(self, *args: str) -> List[str]:
        """
        Run an ffmpeg command, text output.

        :param args: ffmpeg arguments

        :return:     stdout of ffmpeg
        """
        logger.debug("ffmpeg command args: {}".format(" ".join(list(args))))
        return run([self.ffmpeg_path] + self.ffargs + list(args), capture_output=True, check=True, text=True) \
            .stdout.splitlines()

    def ffprobe_json(self, *args: str) -> Dict[str, Any]:
        """
        Run an ffprobe command, get json output.

        :param args: ffprobe arguments

        :return:     json output
        """
        ffout = run([self.ffprobe_path] + self.ffargs + ["-print_format", "json"] + list(args),
                    capture_output=True, check=True, text=True)
        return json.loads(ffout.stdout)

    def find_ffmpeg(self, search_path: Optional[str] = None) -> None:
        """
        Find the ffmpeg and ffprobe binaries.

        :param search_path: Path to search for binaries.
        """
        if search_path is None:
            ffmpeg = which("ffmpeg")
            ffprobe = which("ffprobe")
        else:
            search_path = os.path.dirname(search_path) if os.path.isfile(search_path) else search_path
            ffprobe = os.path.join(os.path.dirname(search_path), "ffprobe")
            ffmpeg = os.path.join(os.path.dirname(search_path), "ffmpeg")

        if ffmpeg is None or ffprobe is None or not os.path.isfile(ffmpeg) or not os.path.isfile(ffprobe):
            raise FileNotFoundError(f"eztrim: ffmpeg/ffprobe executables not found in {search_path or 'PATH'}")

        self.ffmpeg_path = ffmpeg
        self.ffprobe_path = ffprobe


class FFmpegAudio(FFmpeg):
    ffprobe_path: str
    ffmpeg_path: str
    ffargs: List[str]

    def get_audio_streams(self, filename: str, select: Union[int, List[int], None] = None) -> List[AudioStream]:
        """
        Load audio stream metadata from file with ffprobe.

        :param filename: File to process.
        :param select:   List of zero-indexed audio streams to get.
                         (Default: None, get all)

        :return:         List of ``AudioStream``\\s
        """
        try:
            json_streams = self.ffprobe_json("-show_streams", "-select_streams", "a", filename)["streams"]
        except CalledProcessError:
            raise ValueError(f"Could not probe \"{filename}\"! Does it exist and is it a media container?") from None
        select = [select] if isinstance(select, int) else select
        streams: List[AudioStream] = []
        for s in json_streams:
            depth = s.get("bits_per_raw_sample", s.get("bits_per_sample", None))
            depth = int(depth) if int(depth) != 0 else None
            codec = self.codecs[s["codec_name"]] if s["codec_name"] in self.codecs else None
            streams.append(AudioStream(codec=codec, stream_index=s["index"], depth=depth))
        return streams if select is None else [streams[i] for i in select]

    def copy_or_decode(self, streams: List[AudioStream]) -> List[str]:
        """
        Decide whether to copy or decode an audio stream,
        and if we need to decode decide what depth.

        :param streams: Audio streams to resolve.

        :return: FFmpeg -c:a:x arguments
        """
        ac: List[str] = []
        for i, s in enumerate(streams):
            ac += [f"-c:a:{i}"]
            if s.codec is None:
                raise ValueError(f"Unsupported codec in stream {s.stream_index} selected!")
            if s.codec.can_encode:
                ac += ["copy"]
            else:
                best = f"pcm_s{s.depth}le" if s.depth in (8, 16, 24, 32, 64) else "pcm_s16le"
                # this warning will be annoying but whatever just silence it :^)
                if s.codec.compression_type == CompressionType.LOSSY:
                    logger.warning(f"Lossy codec {s.codec.name} is unsupported for encoding! Will decode to {best}.")
                elif s.codec.compression_type == CompressionType.EITHER:
                    logger.warning(f"Potentially-lossy codec {s.codec.name} is unsupported for encoding! "
                                   f"Will decode to {best}.")
                else:
                    logger.info(f"Lossless codec {s.codec.name} is unsupported for encoding! Will decode to {best}.")
                ac += [best]

        logger.debug("selected codecs: {}".format(" ".join(ac)))
        return ac

    def map_streams(self, streams: List[AudioStream], output: Union[str, List[str]],
                    filename: str = "", combine: bool = True) -> List[str]:
        """
        Generate a set of ffmpeg map arguments for a given set of streams
        and a given set out output filenames.

        :param filename:  Container file to process.
        :param streams:   List of audio streams to map.
        :param outfile:   Output file. If multiple streams are supplied, must contain
                          either the format specifier ``index`` or be a list, unless
                          ``combine`` is True. May contain ``filename`` format specifier.
                          If not present, ".mka" will be appended.
        :param filename:  Filename for outfile formatting.
        :param combine:   Only map to one output file (Default: True)
        """
        output = [output] if isinstance(output, str) else output
        output = [o + ".mka" if not o.lower().endswith(".mka") else o for o in output]

        ffmap: List[str] = []

        if not combine:
            if len(streams) > 1:
                if len(output) > 1 and len(streams) != len(output):
                    raise ValueError("Improper number of output filenames supplied!")
                if len(streams) != len(output) and \
                        not any([name == "index" for _, name, _, _ in string.Formatter().parse(output[0])]):
                    raise ValueError("Output filename does not have an index format specifier!")
                if len(output) == 1:
                    output = [output[0]] * len(streams)
            output = [o.format(filename=filename, index=i) for o, i in zip(output, streams)]
            for o, s in zip(output, streams):
                ffmap += ["-map", f"0:{s.stream_index}", o]
        else:
            if len(output) > 1:
                raise ValueError("Received too many output filenames!")
            if any([name == "index" for _, name, _, _ in string.Formatter().parse(output[0])]):
                raise ValueError("Found an index format specifier in output filename, but 'combine' is True!")
            for s in streams:
                ffmap += ["-map", f"0:{s.stream_index}"]
            ffmap += [output[0].format(filename=filename)]

        logger.debug("ffmap: {}".format(" ".join(ffmap)))
        return ffmap

    def clip_single(self, filename: str, start: float, end: float, streams: List[AudioStream]) -> str:
        """
        Clip a single audio segment from a file.

        :param filename: File to clip
        :param start:    Start time
        :param end:      End time
        :param streams:  Streams to clip

        :return:         Tempfile path containing clipped audio.
        """
        if start >= end or start < 0:
            raise ValueError("Invalid clip range")
        out = get_temp_filename(prefix="_acsuite_temp_", suffix=".mka")
        try:
            self.ffmpeg("-i", filename,
                        "-ss", str(start),
                        "-to", str(end),
                        *self.copy_or_decode(streams),
                        *self.map_streams(streams, out), "-y")
        except CalledProcessError:
            os.remove(out) if os.path.isfile(out) else None
            raise ValueError(f"Could not clip \"{filename}\"! Does it exist and is it a media container?") from None
        return out

    def concat(self, *files: str) -> str:
        """
        Concatenate files.

        :param files: Files to concatenate.

        :return:      Tempfile path containing concatenated audio.
        """
        cf = get_temp_filename(prefix="_acsuite_temp_", suffix=".txt")
        out = get_temp_filename(prefix="_acsuite_temp_", suffix=".mka")
        with open(cf, "w") as cfo:
            for f in files:
                f = f.replace("'", "'\\''")
                cfo.write(f"file '{f}'\n")
        try:
            self.ffmpeg("-f", "concat",
                        "-i", cf,
                        "-c", "copy",
                        out, "-y")
        except CalledProcessError:
            os.remove(cf)
            os.remove(out) if os.path.isfile(out) else None
            raise ValueError("Could not concatenate!") from None
        os.remove(cf)
        return out

    def split(self, filename: str, outfile: Union[str, List[str]]) -> None:
        """
        Split audio streams from a multimedia container into multiple files.

        :param filename:  Input file.
        :param outfile:   Output file. May contain ``filename`` and ``index`` format
                          specifiers. If there are multiple streams, either an ``index``
                          formatter or a sufficient number of filenames must be supplied.
                          If not present, ".mka" will be appended.
        """
        streams = self.get_audio_streams(filename)
        self.ffmpeg("-i", filename,
                    "-c", "copy",
                    "-vn", "-sn",
                    *self.map_streams(streams, outfile, filename, combine=False),
                    "-y"
                    )

    def join(self, outfile: str, *filenames: str) -> None:
        """
        Join split audio files into a single multimedia container.

        :param outfile:   Output file.
        :param filenames: Files to join into container.
        """
        self.ffmpeg(*[x for y in list(zip(["-i"]*len(filenames), filenames)) for x in y],
                    *[x for y in list(zip(["-map"]*len(filenames), [f"{i:d}:a" for i in range(len(filenames))]))
                      for x in y],
                    "-c", "copy",
                    outfile,
                    "-y"
                    )

    def recut(self, filename: str, ranges: List[Tuple[float, float]], streams: List[AudioStream],
              outfile: Union[str, List[str]] = "{filename}_ATrim.mka", combine: bool = True) -> List[str]:
        """
        Recut audio from a multimedia container.

        :param filename:  Container file to process.
        :param ranges:    Ranges to trim and append.
        :param streams:   Streams to trim. Zero-indexed, only considers audio streams.
                          If a file is 0:Video, 1:Audio, 2:Audio then stream 1 will be 0
                          and stream 2 will be 1. If ``None``, process all streams
                          (Default: None).
        :param outfile:   Output file. If multiple streams are selected, must contain
                          either the format specifier ``index`` or be a list, unless
                          ``combine`` is True. May contain ``filename`` format specifier.
                          If not present, ".mka" will be appended.
                          (Default: "{filename}_ATrim.mka")
        :param combine:   Output all trimmed streams into a single file. (Default: True)
        """
        outfile = [outfile] if isinstance(outfile, str) else outfile
        outfile = [o + ".mka" if not o.lower().endswith(".mka") else o for o in outfile]
        partials: List[str] = []

        try:
            # generate clips
            # can't use a list comprehension here otherwise we'll lose the
            # entire list for cleanup
            for r in ranges:
                partials.append(self.clip_single(filename, r[0], r[1], streams))

            # concatenate, if necessary
            clipped = self.concat(*partials) if len(ranges) > 1 else partials[0]

            # split and rename
            if not combine and len(streams) > 1:
                self.split(clipped, outfile)
                if len(outfile) != len(streams):
                    outfile = [outfile[0]] * len(streams)
            else:
                if len(outfile) > 1:
                    raise ValueError("Received too many output filenames!")
                if any([name == "index" for _, name, _, _ in string.Formatter().parse(outfile[0])]):
                    raise ValueError("Found an index format specifier in output filename, but 'combine' is True!")
                os.rename(clipped, outfile[0].format(filename=filename))
        finally:
            for p in partials:
                os.remove(p) if os.path.isfile(p) else None
            os.remove(clipped) if os.path.isfile(clipped) else None

        return [o.format(filename=filename, index=i) for o, i in zip(outfile, streams)]
