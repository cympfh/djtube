"""Paint a fixed-size picture of the decks that are playing.

The browser does not send pixels. It only says which videos are audible.
Thumbnails are drawn here, once per picture, and the encoder repeats the
frame. A change of which videos are showing fades quickly. A change of gain
does not: the crossfader should move the picture as it moves the sound.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

from PIL import Image, ImageFilter

VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
# A still does not need a film frame rate. 5 fps leaves a 0.4 s fade two
# frames long, which is enough to see a cut without staying on it.
VIDEO_FPS = 5
FADE_SECONDS = 0.4
_PAD = 0.08
# Background sits under the sharp picture. 0.55 toward black keeps the
# blurred cover visible without competing with the centered image.
_DIM = 0.55


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _paste_contained(base: Image.Image, image: Image.Image, pad: float = _PAD) -> None:
    width, height = base.size
    box_w = max(1, int(width * (1 - 2 * pad)))
    box_h = max(1, int(height * (1 - 2 * pad)))
    scale = min(box_w / image.width, box_h / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    base.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))


def title_card(size: tuple[int, int] = (VIDEO_WIDTH, VIDEO_HEIGHT)) -> Image.Image:
    """Solid black, for when nothing is on the decks. No title text."""

    return Image.new("RGB", size, (0, 0, 0))


def deck_card(image: Image.Image, size: tuple[int, int] = (VIDEO_WIDTH, VIDEO_HEIGHT)) -> Image.Image:
    """One picture, aspect kept, on a blurred and darkened copy of itself."""

    source = image.convert("RGB")
    background = _cover(source, size)
    radius = max(2, size[1] // 36)
    background = background.filter(ImageFilter.GaussianBlur(radius=radius))
    black = Image.new("RGB", size, (0, 0, 0))
    frame = Image.blend(background, black, _DIM)
    _paste_contained(frame, source)
    return frame


def mix_cards(cards: Sequence[Image.Image], gains: Sequence[float]) -> Image.Image:
    """Stack one or two cards. Two cards crossfade by relative gain."""

    if not cards:
        raise ValueError("no cards")
    if len(cards) == 1 or len(gains) < 2:
        return cards[0]
    total = float(gains[0]) + float(gains[1])
    alpha = 0.5 if total <= 0 else float(gains[1]) / total
    alpha = min(1.0, max(0.0, alpha))
    return Image.blend(cards[0], cards[1], alpha)


class Composer:
    """Render the current decks. Video-id changes fade; gain changes do not."""

    def __init__(self, size: tuple[int, int] = (VIDEO_WIDTH, VIDEO_HEIGHT), fade: float = FADE_SECONDS) -> None:
        self.size = size
        self.fade = fade
        self._cards: dict[tuple[str, int], Image.Image] = {}
        self._title = title_card(size)
        self._ids: tuple[str, ...] | None = None
        self._current: Image.Image | None = None
        self._from: Image.Image | None = None
        self._from_at = 0.0

    def frame(
        self,
        decks: Sequence[tuple[str, float]],
        images: Mapping[str, Image.Image],
        now: float | None = None,
    ) -> Image.Image:
        moment = time.monotonic() if now is None else now
        shown = [(video, gain) for video, gain in decks if video in images][:2]
        ids = tuple(video for video, _gain in shown)
        target = self._layout(shown, images)
        if ids != self._ids:
            if self._current is not None:
                self._from = self._current
                self._from_at = moment
            self._ids = ids
        self._current = target
        if self._from is None:
            return target
        if self.fade <= 0:
            self._from = None
            return target
        span = (moment - self._from_at) / self.fade
        if span >= 1:
            self._from = None
            return target
        return Image.blend(self._from, target, min(1.0, max(0.0, span)))

    def _layout(self, shown: Sequence[tuple[str, float]], images: Mapping[str, Image.Image]) -> Image.Image:
        if not shown:
            return self._title
        cards = [self._card(video, images[video]) for video, _gain in shown]
        return mix_cards(cards, [gain for _video, gain in shown])

    def _card(self, video: str, image: Image.Image) -> Image.Image:
        key = (video, id(image))
        cached = self._cards.get(key)
        if cached is None:
            cached = deck_card(image, self.size)
            self._cards[key] = cached
            if len(self._cards) > 4:
                self._cards.pop(next(iter(self._cards)))
        return cached
