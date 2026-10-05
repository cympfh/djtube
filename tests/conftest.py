from __future__ import annotations

import pytest

from djtube.audio import AudioCache, current_audio_cache, install_audio_cache


@pytest.fixture(autouse=True)
def isolated_audio_cache(tmp_path):
    previous = current_audio_cache()
    install_audio_cache(AudioCache(tmp_path / "isolated-audio-cache.json"))
    try:
        yield
    finally:
        install_audio_cache(previous)
