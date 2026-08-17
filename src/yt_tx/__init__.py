"""yt-tx: YouTube transcript harvester.

Enumerate channels, persist metadata to MySQL, download transcripts where they
exist, and record enough state that a re-run never redoes completed work.
Videos with no transcript stay open with ``needs_audio=1`` for a later
audio-to-text pass.
"""

from __future__ import annotations

__version__ = "1.0.0"
