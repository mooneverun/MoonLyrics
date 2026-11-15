"""Choose writable per-user storage, with a persistent portable fallback."""
from pathlib import Path
from uuid import uuid4


def application_directory(executable, source_directory, *, frozen, launcher_directory=None):
    """A disposable single-file payload must keep its profile beside the outer EXE."""
    if not frozen:
        return Path(source_directory)
    if launcher_directory:
        candidate = Path(launcher_directory)
        if candidate.is_absolute() and candidate.is_dir():
            return candidate
    return Path(executable).resolve().parent


def _check_writable(directory):
    directory.mkdir(parents=True, exist_ok=True)
    # tempfile treats some Windows access-denied errors as name collisions and
    # can retry billions of times. One unique, exclusive create is sufficient.
    probe = directory / (".moonlyrics-write-check-" + uuid4().hex)
    created = False
    try:
        with probe.open("xb"):
            created = True
    finally:
        if created:
            probe.unlink()


def data_directory(preferred, application_directory):
    preferred = Path(preferred)
    portable = Path(application_directory) / "MoonLyrics-data"
    # Once a portable profile is created, continue using it on later launches.
    candidates = (portable, preferred) if portable.is_dir() else (preferred, portable)
    error = None
    for directory in candidates:
        try:
            _check_writable(directory)
            return directory
        except OSError as exc:
            error = exc
    raise OSError("MoonLyrics cannot write its data folder; extract it to a writable folder.") from error
