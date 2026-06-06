"""
ui - A minimal retained-mode, dirty-rectangle UI engine for the HiFi display.

    from ui import WindowManager, VBox, HBox, Label, Button, VolumeFader

Layers:
    core     Widget/Container base, dirty-cache lifecycle, compositor, easing.
    layout   VBox / HBox / ZStack / Spacer automatic positioning.
    widgets  SolidRect, Label, Icon, Button, ProgressBar, VolumeFader.
"""

from .core import (
    # easing / animation
    linear, ease_in, ease_out, ease_in_out, ease_out_cubic,
    lerp, lerp_color, Animator,
    # input
    event_pos, is_down, is_up, is_move, set_screen_size, get_screen_size,
    # base + compositor
    Widget, Container, merge_rects, WindowManager,
)
from .layout import Spacer, VBox, HBox, ZStack
from .widgets import (
    SolidRect, Label, Icon, Button, ProgressBar, VolumeFader,
)

__all__ = [
    "linear", "ease_in", "ease_out", "ease_in_out", "ease_out_cubic",
    "lerp", "lerp_color", "Animator",
    "event_pos", "is_down", "is_up", "is_move",
    "set_screen_size", "get_screen_size",
    "Widget", "Container", "merge_rects", "WindowManager",
    "Spacer", "VBox", "HBox", "ZStack",
    "SolidRect", "Label", "Icon", "Button", "ProgressBar", "VolumeFader",
]
