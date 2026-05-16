"""Совместимость: используйте image_finder."""

from image_finder import find_event_image as resolve_event_poster
from image_finder import regenerate_event_image as regenerate_poster_only

__all__ = ["resolve_event_poster", "regenerate_poster_only"]
