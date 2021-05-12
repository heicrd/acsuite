import vapoursynth as vs

from fractions import Fraction
from functools import lru_cache
from typing import List, Optional, Union, Tuple, cast

from .types import Trim


def get_timecodes(timecodes_file: Optional[str] = None, clip: Optional[vs.VideoNode] = None) -> List[float]:
    """
    Get timecodes for every frame.

    Takes either a timecodes file to load or a clip to use as a reference.
    At least one must be provided.

    :param timecodes_file: Path to v2 timecodes plaintext file.
    :param clip:           Reference vapoursynth clip. If vfr, the timecodes will be calculated.

    :return:               List of timecodes.
    """
    if timecodes_file is not None:
        return [float(x) / 1000 for x in open(timecodes_file, "r").read().splitlines()[1:]]

    if clip is None:
        raise ValueError("get_timecodes: need a clip or timecodes file")

    if clip.fps == Fraction(0, 1):
        return clip_to_timecodes(clip)

    return [round(float(1e9*f*(1/clip.fps)))/1e9 for f in range(0, clip.num_frames + 1)]


def frames_to_timecodes(ranges: Union[Trim, List[Trim]],
                        timecodes: List[float]) -> List[Tuple[float, float]]:
    """
    Convert a list of frame ranges to a list of timestamp ranges.

    :param ranges:    List of frame ranges to validate and convert.
    :param timecodes: Timecodes for each frame in the clip.

    :return:          List of timestamp ranges.
    """
    ranges = [ranges] if isinstance(ranges, tuple) else ranges
    num_frames = len(timecodes) - 1

    out = []
    for r in ranges:
        start, end = r
        start = 0 if start is None else start
        start = start + num_frames if start < 0 else start
        end = num_frames if end is None else end
        end = end + num_frames if end <= 0 else end
        if start >= end:
            raise ValueError("frames_to_timecodes: start frame is later than end frame")
        out.append((timecodes[start], timecodes[end]))

    return out


@lru_cache
def clip_to_timecodes(src_clip: vs.VideoNode) -> List[float]:
    """
    Cached function to return a list of timecodes for vfr clips.

    The first call to this function can be `very` expensive depending on the `src_clip`
    length and the source filter used.

    Subsequent calls on the same clip will return the previously generated list of timecodes.
    The timecodes are `floats` representing seconds from the start of the `src_clip`.

    If you have ``rich`` installed, will output a pretty progress bar as this process can take a long time.
    """
    try:
        from rich.progress import track
        rich = True
    except ImportError:

        def track(x, description, total):  # type: ignore
            return x

        rich = False

    timecodes = [0.0]
    curr_time = Fraction()
    for frame in track(src_clip.frames(), description="Finding timestamps...", total=src_clip.num_frames):
        num = cast(int, frame.props["_DurationNum"])
        den = cast(int, frame.props["_DurationDen"])
        curr_time += Fraction(num, den)
        timecodes.append(float(curr_time))
        if rich:
            pass  # if ran in a normal console/terminal, should render a pretty progress bar
        else:
            print(f"Generating timecodes: {round(100 * len(timecodes) / src_clip.num_frames)}%", end="\r")
    print("")
    return timecodes
