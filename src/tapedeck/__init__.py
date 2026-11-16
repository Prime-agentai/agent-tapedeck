"""tapedeck: record and replay coding-agent CLIs (claude, codex, gemini) for deterministic tests."""

__version__ = "0.2.0"

from .cassette import Cassette, CassetteError, Effect, Event, Interaction  # noqa: E402
from .deck import Deck, use_cassette  # noqa: E402
from .diff import CassetteDiff, diff_cassettes  # noqa: E402

__all__ = ["Cassette", "CassetteDiff", "CassetteError", "Deck", "diff_cassettes", "Effect", "Event", "Interaction", "use_cassette", "__version__"]
