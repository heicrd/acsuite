"""Frame-based cutting/trimming/splicing of audio with VapourSynth and FFmpeg."""
__all__ = ["clip_to_timecodes", "concat", "eztrim", "f2ts"]
try:
    from ._metadata import __author__, __credits__, __date__, __version__
except ImportError:
    __author__ = __credits__ = __date__ = __version__ = "unknown (portable mode)"

import collections
import fractions
import functools
import os
from shutil import which
from subprocess import run
from typing import Deque, Dict, List, Optional, Tuple, Union
from warnings import simplefilter, warn

import vapoursynth as vs

simplefilter("always")  # display warnings

Trim = Tuple[Optional[int], Optional[int]]

# fmt: off
VALID_FFMPEG_EXTENSIONS = [
    '.aac', '.m4a', '.adts',
    '.ac3',
    '.alac', '.caf',
    '.dca', '.dts',
    '.eac3',
    '.flac',
    '.gsm',
    '.mlp',
    '.mp2', '.mp3', '.mpga',
    '.opus', '.spx', '.ogg', '.oga',
    '.pcm', '.raw',
    '.sbc',
    '.thd',
    '.tta',
    '.wav', '.w64',
    '.wma',
]
# fmt: on


def eztrim(
    clip: vs.VideoNode,
    /,
    trims: Union[List[Trim], Trim],
    audio_file: str,
    outfile: Optional[str] = None,
    *,
    ffmpeg_path: Optional[str] = None,
    quiet: bool = False,
    timecodes_file: Optional[str] = None,
    debug: bool = False,
) -> Union[Dict, str, None]:
    """
    Simple trimming function that follows VapourSynth/Python slicing syntax.

    End frame is NOT inclusive.

    For a 100 frame long VapourSynth clip:

    >>> src = core.ffms2.Source('file.mkv')
    >>> clip = src[3:22]+src[23:40]+src[48]+src[50:-20]+src[-10:-5]+src[97:]
    >>> 'These trims can be almost directly entered as:'
    >>> trims = [(3, 22), (23, 40), (48, 49), (50, -20), (-10, -5), (97, None)]
    >>> eztrim(src, trims, 'audio_file.wav')

    >>> src = core.ffms2.Source('file.mkv')
    >>> clip = src[3:-13]
    >>> 'A single slice can be entered as a single tuple:'
    >>> eztrim(src, (3, -13), 'audio_file.wav')


    :param clip:          Input clip needed to determine framerate for audio timecodes
                          and ``clip.num_frames`` for negative indexing.
    :param trims:         Either a list of 2-tuples, or one tuple of 2 ints.

        Empty slicing must represented with a ``None``.
            ``src[:10]+src[-5:]`` must be entered as ``trims=[(None, 10), (-5, None)]``.

        Single frame slices must be represented as a normal slice.
            ``src[15]`` must be entered as ``trims=(15, 16)``.

    :param audio_file:    A string to the source audio file's location
                          (i.e. '/path/to/audio_file.ext').
                          If the extension is not recognized as a valid audio file extension for FFmpeg's encoders,
                          the audio will be re-encoded to WAV losslessly.

    :param outfile:       Either a filename 'out.ext' or a full path '/path/to/out.ext'
                          that will be used for the trimmed audio file.
                          The extension will be automatically inserted for you,
                          and if it is given, it will be overwritten by the input `audio_file`'s extension.
                          If left blank, defaults to ``audio_file_cut.ext``.

    :param ffmpeg_path: Set this if ``ffmpeg`` is not in your `PATH`.
                        If ``ffmpeg`` exists in your `PATH`, it will automatically be detected and used.

    :param quiet:         Suppresses most console output from FFmpeg.

    :param timecodes_file: Timecodes v2 file (generated by vspipe, ffms2, etc.) for variable-frame-rate clips.
                           Not needed for CFR clips.

    :param debug:         Used for testing purposes.

    :return: Returns output file name as a string for other functions.
    """
    if debug:
        pass
    else:
        # --- checking for filename issues and file extension support --------------------------------------------------
        if not os.path.isfile(audio_file):
            raise FileNotFoundError(f"eztrim: {audio_file} not found")
        audio_file_name, audio_file_ext = os.path.splitext(audio_file)
        codec_args = []

        if audio_file_ext in VALID_FFMPEG_EXTENSIONS:
            codec_args += ["-c:a", "copy", "-rf64", "auto"]
        else:
            warn(
                f"eztrim: {audio_file_ext} is not a supported extension by FFmpeg's audio encoders, re-encoding to WAV",
                Warning,
            )
            audio_file_ext = ".wav"  # defaults to pcm_s16le so a 24-bit input with wrong ext will be downscaled

        # --- re-naming outfile if not formatted correctly -------------------------------------------------------------
        if outfile is None:
            outfile = audio_file_name + "_cut" + audio_file_ext
        elif not os.path.splitext(outfile)[1]:
            outfile += audio_file_ext
        elif os.path.splitext(outfile)[1] != audio_file_ext:
            outfile = os.path.splitext(outfile)[0] + audio_file_ext

        if os.path.isfile(outfile):
            raise FileExistsError(f"eztrim: {outfile} already exists")

        # --- checking for ffmpeg --------------------------------------------------------------------------------------
        if ffmpeg_path is None:
            if not which("ffmpeg"):
                raise FileNotFoundError("concat: ffmpeg executable not found in PATH")
            else:
                ffmpeg_path = which("ffmpeg")
        else:
            if not os.path.isfile(ffmpeg_path):
                raise FileNotFoundError(f"concat: ffmpeg executable at {ffmpeg_path} not found")

        # --- timecodes ------------------------------------------------------------------------------------------------

        if (timecodes_file is not None) and (not os.path.isfile(timecodes_file)):
            raise FileNotFoundError(f"eztrim: {timecodes_file} not found")

    # --- trims --------------------------------------------------------------------------------------------------------
    if not isinstance(trims, (list, tuple)):
        raise TypeError("eztrim: trims must be a list of 2-tuples (or just one 2-tuple)")

    if len(trims) == 1 and isinstance(trims, list):
        warn(
            "eztrim: using a list of one 2-tuple is not recommended; for a single trim,"
            "directly use a tuple: `trims=(5,-2)` instead of `trims=[(5,-2)]`",
            SyntaxWarning,
        )
        if isinstance(trims[0], tuple):
            trims = trims[0]  # convert nested tuple in a list to just the tuple
            if len(trims) != 2:
                raise ValueError("eztrim: a single tuple trim must have 2 elements")
            if trims[-1] == 0:
                raise ValueError("eztrim: slices cannot end with 0, if attempting to use an empty slice, use `None`")
            if trims == (None, None):
                warn("eztrim: None, None slice will cause no trimming, quitting early")
                return outfile
        else:
            raise ValueError("eztrim: the inner trim must be a tuple")
    elif isinstance(trims, list):
        for trim in trims:
            if not isinstance(trim, tuple):
                raise TypeError(f"eztrim: the trim {trim} is not a tuple")
            if len(trim) != 2:
                raise ValueError(f"eztrim: the trim {trim} needs 2 elements")
            for i in trim:
                if not isinstance(i, (int, type(None))):
                    raise ValueError(f"eztrim: the trim {trim} must have 2 ints or None's")
            if trim[-1] == 0:
                raise ValueError("eztrim: slices cannot end with 0, if attempting to use an empty slice, use `None`")

    # ------------------------------------------------------------------------------------------------------------------

    num_frames = clip.num_frames
    ts = functools.partial(f2ts, timecodes_file=timecodes_file, src_clip=clip)
    ffmpeg_silence = [ffmpeg_path, "-hide_banner", "-loglevel", "16"] if quiet else [ffmpeg_path, "-hide_banner"]

    # --- single trim --------------------------------------------------------------------------------------------------
    if isinstance(trims, tuple):
        start, end = _negative_to_positive(num_frames, *trims)
        if end <= start:
            raise ValueError("eztrim: the trim is not logical")
        debug_dict = {"s": start, "e": end}
        args = ffmpeg_silence + ["-i", audio_file, "-vn", "-ss", ts(start), "-to", ts(end)] + codec_args + [outfile]
        debug_dict.update({"args": args})
        if debug:
            return debug_dict
        run(args)
        return outfile

    # --- multiple trims with concatenation ----------------------------------------------------------------------------
    starts, ends = _negative_to_positive(num_frames, [s for s, e in trims], [e for s, e in trims])
    if not _check_ordered(starts, ends):
        raise ValueError("eztrim: the trims are not logical")

    if os.path.isfile("_acsuite_temp_concat.txt"):
        raise ValueError("eztrim: _acsuite_temp_concat.txt already exists, quitting")
    else:
        concat_file = open("_acsuite_temp_concat.txt", "w")
        temp_filelist = []
    times = zip([ts(f) for f in starts], [ts(f) for f in ends])
    for key, time in enumerate(times):
        outfile_tmp = f"_acsuite_temp_output_{key}" + os.path.splitext(outfile)[-1]
        concat_file.write(f"file {outfile_tmp}\n")
        temp_filelist.append(outfile_tmp)
        args = ffmpeg_silence + ["-i", audio_file, "-vn", "-ss", time[0], "-to", time[1]] + codec_args + [outfile_tmp]
        debug_dict.update({f"args_{key}": args})
        if debug:
            return debug_dict
        run(args)

    concat_file.close()
    args = ffmpeg_silence + ["-f", "concat", "-i", "_acsuite_temp_concat.txt", "-c", "copy", outfile]
    run(args)

    os.remove("_acsuite_temp_concat.txt")
    for file in temp_filelist:
        os.remove(file)

    return outfile


def f2ts(f: int, /, *, precision: int = 3, timecodes_file: Optional[str] = None, src_clip: vs.VideoNode) -> str:
    """
    Converts frame number to a timestamp based on framerate.

    Can handle variable-frame-rate clips as well, using similar methods to that of ``vspipe --timecodes``.
    For VFR clips, will use a timecodes v2 file if given, else will fallback to the slower ``src_clip.frames()`` method.
    Meant to be called as a ``functools.partial`` with `src_clip` specified before-hand.

    :param f: Frame number (indexed from ``0``). Can be negative, indexing from the last frame of the `src_clip`.
    :param precision: An integer in ``[0, 3, 6, 9]`` representing the precision of the timestamp
                      (second, millisecond, microsecond, nanosecond respectively).
    :param timecodes_file: An optional path to a v2 timecodes plaintext file for VFR clips (not used for CFR clips).
                           If not given, will fallback to a `much` slower method of determining each frame's timestamp.
    :param src_clip: A VapourSynth clip for determining the timestamp.
                     ``src_clip.fps`` is used for CFR clips, and the frame props
                     (``_DurationNum`` and ``_DurationDen``) are used for VFR clips if a `timecodes_file` is not given.

    :return: A string representing the timestamp of the requested frame number.
    """
    if precision not in [0, 3, 6, 9]:
        raise ValueError(f"f2ts: the precision {precision} must be a multiple of 3 (including 0)")

    if f < 0:
        f += src_clip.num_frames

    if f == 0:
        s = 0
    elif src_clip.fps != fractions.Fraction(0, 1):
        t = round(10 ** 9 * f * src_clip.fps ** -1)
        s = t / 10 ** 9
    else:
        if timecodes_file is not None:
            timecodes = [float(x) / 1000 for x in open(timecodes_file, "r").read().splitlines()[1:]]
            s = timecodes[f]
        else:
            s = clip_to_timecodes(src_clip)[f]

    m = s // 60
    s %= 60
    h = m // 60
    m %= 60

    if precision == 0:
        return f"{h:02.0f}:{m:02.0f}:{round(s):02}"
    elif precision == 3:
        return f"{h:02.0f}:{m:02.0f}:{s:06.3f}"
    elif precision == 6:
        return f"{h:02.0f}:{m:02.0f}:{s:09.6f}"
    elif precision == 9:
        return f"{h:02.0f}:{m:02.0f}:{s:012.9f}"


@functools.lru_cache
def clip_to_timecodes(src_clip: vs.VideoNode) -> Deque[float]:
    """
    Cached function to return a list of timecodes for vfr clips.

    The first call to this function can be `very` expensive depending on the `src_clip`
    length and the source filter used.

    Subsequent calls on the same clip will return the previously generated list of timecodes.
    The timecodes are `floats` representing seconds from the start of the `src_clip`.

    If you have ``rich`` installed, will output a pretty progress bar as this process can take a long time.
    """
    # fmt: off
    try:
        from rich.progress import track
        rich = True
    except ImportError:
        track = lambda x, description, total: x
        rich = False
    # fmt: on
    timecodes = collections.deque([0.0], maxlen=src_clip.num_frames + 1)
    curr_time = fractions.Fraction()
    init_percentage = 0
    for frame in track(src_clip.frames(), description="Finding timestamps...", total=src_clip.num_frames):
        curr_time += fractions.Fraction(frame.props["_DurationNum"], frame.props["_DurationDen"])
        timecodes.append(float(curr_time))
        if rich:
            pass  # if ran in a normal console/terminal, should render a pretty progress bar
        else:
            percentage_done = round(100 * len(timecodes) / src_clip.num_frames)
            if percentage_done % 10 == 0 and percentage_done != init_percentage:
                print(rf"Finding timecodes for variable-framerate clip: {percentage_done}% done")
                init_percentage = percentage_done
    return timecodes


_Neg2pos_in = Union[List[Optional[int]], Optional[int]]
_Neg2pos_out = Union[Tuple[List[int], List[int]], Tuple[int, int]]


def _negative_to_positive(num_frames: int, a: _Neg2pos_in, b: _Neg2pos_in) -> _Neg2pos_out:
    """Changes negative/zero index to positive based on num_frames."""
    single_trim = isinstance(a, (int, type(None))) and isinstance(b, (int, type(None)))

    # --- single trim --------------------------------------------------------------------------------------------------
    if single_trim:
        a, b = (a or 0), (b or 0)
        if abs(a) > num_frames or abs(b) > num_frames:
            raise ValueError(f"_negative_to_positive: {max(abs(a), abs(b))} is out of bounds")
        return a if a >= 0 else num_frames + a, b if b > 0 else num_frames + b

    # --- multiple trims -----------------------------------------------------------------------------------------------
    if len(a) != len(b):
        raise ValueError("_negative_to_positive: lists must be same length")

    real_a, real_b = [(i or 0) for i in a], [(i or 0) for i in b]  # convert None to 0

    if not (all(abs(i) <= num_frames for i in real_a) and all(abs(i) <= num_frames for i in real_b)):
        raise ValueError("_negative_to_positive: one or more trims are out of bounds")

    if all(i >= 0 for i in real_a) and all(i > 0 for i in real_b):
        return real_a, real_b

    positive_a = [x if x >= 0 else num_frames + x for x in real_a]
    positive_b = [y if y > 0 else num_frames + y for y in real_b]

    return positive_a, positive_b


def _check_ordered(starts: List[int], ends: List[int]) -> bool:
    """Checks if lists follow logical Python slicing."""
    if not all(starts[i] < ends[i] for i in range(len(starts))):
        return False
    if not all(ends[i] < starts[i + 1] for i in range(len(starts) - 1)):
        warn("_check_ordered: one or more trims will cause overlapping", Warning)
    return True


def concat(audio_files: List[str], outfile: str, *, ffmpeg_path: Optional[str] = None, quiet: bool = False) -> None:
    """Function to concatenate mutliple audio files.

    All audio files must have the same extension, and the outfile must have the same extension as the audio files.

    :param audio_files: List of strings representing audio file paths (i.e. ``['file1.wav', 'file2.wav']``).
    :param outfile: String representing desired filename for the concatenated audio.
    :param ffmpeg_path: Set this if ``ffmpeg`` is not in your `PATH`.
                        If ``ffmpeg`` exists in your `PATH`, it will automatically be detected and used.
    :param quiet: Suppresses most console output from FFmpeg.
    """
    # --- checking for ffmpeg ------------------------------------------------------------------------------------------
    if ffmpeg_path is None:
        if not which("ffmpeg"):
            raise FileNotFoundError("concat: ffmpeg executable not found in PATH")
        else:
            ffmpeg_path = which("ffmpeg")
    else:
        if not os.path.isfile(ffmpeg_path):
            raise FileNotFoundError(f"concat: ffmpeg executable at {ffmpeg_path} not found")

    # --- checking for filename issues and file extension support ------------------------------------------------------
    if len(audio_files) < 2:
        raise ValueError("concat: requires 2 or more audio files to concatenate")
    audio_file_extensions = set([os.path.splitext(af)[1] for af in audio_files] + [os.path.splitext(outfile)[1]])
    if len(audio_file_extensions) > 1:
        raise ValueError("concat: all files must have the same extension")
    if (ext := audio_file_extensions.pop()) not in VALID_FFMPEG_EXTENSIONS:
        raise ValueError(f"concat: '{ext}' is not a valid extension recognized by any known FFmpeg encoders")
    for af in audio_files:
        if not os.path.isfile(af):
            raise FileNotFoundError(f"concat: {af} not found")
    if os.path.isfile(outfile):
        raise FileExistsError(f"eztrim: {outfile} already exists")
    # ------------------------------------------------------------------------------------------------------------------

    ffmpeg_silence = [ffmpeg_path, "-hide_banner", "-loglevel", "16"] if quiet else [ffmpeg_path, "-hide_banner"]

    if os.path.isfile("_acsuite_temp_concat.txt"):
        raise ValueError("concat: _acsuite_temp_concat.txt already exists, quitting")
    concat_file = open("_acsuite_temp_concat.txt", "w")
    for af in audio_files:
        concat_file.write(f"file {af}\n")

    concat_file.close()
    args = ffmpeg_silence + ["-f", "concat", "-i", "_acsuite_temp_concat.txt", "-c", "copy", outfile]
    run(args)

    os.remove("_acsuite_temp_concat.txt")
