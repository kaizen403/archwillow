"""
Waveform visualization - shows microphone input as animated vertical bars.
"""

import math
import time
import cairo
import numpy as np
from .base import BaseVisualization, StateManager, VisualizerState
from ..theme import theme


class WaveformVisualization(BaseVisualization):
    """
    A clean, chip-style bar audio visualization.

    Displays vertical bars that rise and fall with the audio input,
    creating a dynamic visual representation of sound.

    Colors are loaded from the Omarchy theme.
    """

    def __init__(self):
        super().__init__()

        # Bar settings - compact chip-style OSD
        self.num_bars = 12
        self.bar_width = 4
        self.bar_gap = 2
        self.min_bar_height = 1

        # Amplification for more visible response
        self.amplification = 8.0

        # Smoothing for bar heights (makes animation smoother)
        self.bar_heights = np.zeros(self.num_bars)
        self.decay_rate = 0.75  # How fast bars fall
        self.rise_rate = 0.7    # How fast bars rise

        # State manager for visualizer states (recording, paused, processing, etc.)
        self.state_manager = StateManager()

        # Elapsed time tracking for long-form mode
        self._recording_start_time = None
        self._elapsed_seconds = 0.0
        self._show_elapsed_time = False

    def update(self, level: float, samples: np.ndarray = None):
        """Update with new audio samples."""
        super().update(level, samples)

        if samples is not None and len(samples) > 0:
            # Calculate bar heights from audio samples
            # Divide samples into chunks for each bar
            chunk_size = len(samples) // self.num_bars
            if chunk_size > 0:
                new_heights = np.zeros(self.num_bars)
                for i in range(self.num_bars):
                    start = i * chunk_size
                    end = start + chunk_size
                    chunk = samples[start:end]
                    # Use RMS of chunk for smoother visualization
                    rms = np.sqrt(np.mean(chunk ** 2))
                    new_heights[i] = min(1.0, rms * self.amplification)
                # Boost with overall level so quiet voices still show movement
                if level > 0.01 and np.max(new_heights) < level:
                    new_heights = np.maximum(new_heights, level * 0.8)

                # Smooth transitions - rise fast, fall slow
                for i in range(self.num_bars):
                    if new_heights[i] > self.bar_heights[i]:
                        # Rising - quick response
                        self.bar_heights[i] = (
                            self.rise_rate * new_heights[i] +
                            (1 - self.rise_rate) * self.bar_heights[i]
                        )
                    else:
                        # Falling - slow decay
                        self.bar_heights[i] *= self.decay_rate
                        if self.bar_heights[i] < new_heights[i]:
                            self.bar_heights[i] = new_heights[i]
        else:
            # No audio - decay all bars
            self.bar_heights *= self.decay_rate

        # Update state manager animation
        self.state_manager.update()

    def draw(self, cr: cairo.Context, width: int, height: int):
        """Draw the chip-style bar visualization with no border or dot."""
        chip_height = 36
        chip_radius = chip_height / 2.0

        # Draw chip background
        bg_color = theme.background
        if len(bg_color) == 3:
            bg_rgba = (*bg_color, 0.9)
        else:
            bg_rgba = bg_color
        cr.set_source_rgba(*bg_rgba)
        self._rounded_rectangle(cr, 0, 0, width, chip_height, chip_radius)
        cr.fill()

        # Bars area inside the chip
        padding = 8
        bars_start_x = padding
        bars_width = width - padding * 2
        bars_height = chip_height - padding * 2
        center_y = chip_height / 2.0

        actual_num_bars = self.num_bars
        bar_gap = self.bar_gap
        bar_width = (bars_width - (actual_num_bars - 1) * bar_gap) / actual_num_bars
        total_bar_width = bar_width + bar_gap
        start_x = bars_start_x

        # Get colors from theme (fresh on each draw)
        bar_left = theme.bar_left
        bar_right = theme.bar_right

        # Check if we're in processing state for wave effect
        is_processing = self.state_manager.current_state == VisualizerState.PROCESSING
        wave_phase = self.state_manager.animation_phase if is_processing else 0.0

        # Check if we're in success state for pulse effect
        is_success = self.state_manager.current_state == VisualizerState.SUCCESS
        pulse_value = self.state_manager.get_animation_value() if is_success else 1.0

        # Draw bars
        for i in range(actual_num_bars):
            # Interpolate color from left to right
            t = i / max(1, actual_num_bars - 1)
            r = bar_left[0] * (1 - t) + bar_right[0] * t
            g = bar_left[1] * (1 - t) + bar_right[1] * t
            b = bar_left[2] * (1 - t) + bar_right[2] * t

            # Get normalized bar height (0-1 range)
            normalized_height = self.bar_heights[i]

            # Apply wave pattern during processing state
            if is_processing:
                wave_pos = (i / max(1, actual_num_bars - 1)) * 2 * math.pi + wave_phase
                primary_wave = 0.3 + 0.7 * math.sin(wave_pos)
                harmonic = 0.12 * math.sin(wave_pos * 2)
                wave_modulation = primary_wave + harmonic
                base_height_boost = 0.7
                boosted_normalized = max(normalized_height, base_height_boost)
                normalized_height = boosted_normalized * wave_modulation
                normalized_height = max(0.0, min(1.0, normalized_height))
                bar_h = max(self.min_bar_height, normalized_height * bars_height)
                opacity_modulation = 0.75 + 0.25 * (0.5 + 0.5 * math.sin(wave_pos))
            elif is_success:
                bar_h = max(self.min_bar_height, normalized_height * bars_height)
                pulse_modulation = 0.7 + 0.3 * pulse_value
                bar_h = bar_h * pulse_modulation
                opacity_modulation = pulse_value
            else:
                bar_h = max(self.min_bar_height, normalized_height * bars_height)
                opacity_modulation = 1.0

            x = start_x + i * total_bar_width

            # Draw bar centered vertically
            bar_top = center_y - bar_h / 2.0

            # Draw glow effect
            cr.set_source_rgba(r, g, b, 0.3 * opacity_modulation)
            cr.rectangle(x - 1, bar_top - 1, bar_width + 2, bar_h + 2)
            cr.fill()

            # Draw main bar
            cr.set_source_rgba(r, g, b, 0.9 * opacity_modulation)
            cr.rectangle(x, bar_top, bar_width, bar_h)
            cr.fill()

        # Draw timer centered below the chip
        self._draw_elapsed_time(cr, width, height, chip_height)

    def set_state(self, state_str: str):
        """Set the visualizer state from a string value."""
        self.state_manager.set_state_from_string(state_str)

        # Start/stop elapsed time tracking based on state
        if state_str == 'recording':
            # Always reset elapsed time when starting a new recording
            self._recording_start_time = time.time()
            self._elapsed_seconds = 0.0
            self._show_elapsed_time = True
        elif state_str == 'paused':
            # Keep showing elapsed time but don't increment
            if self._recording_start_time is not None:
                self._elapsed_seconds += time.time() - self._recording_start_time
                self._recording_start_time = None
            self._show_elapsed_time = True
        else:
            # Reset elapsed time for other states
            self._recording_start_time = None
            self._elapsed_seconds = 0.0
            self._show_elapsed_time = False

    def set_elapsed_time(self, seconds: float):
        """Set the elapsed time directly (for long-form mode)."""
        self._elapsed_seconds = seconds
        self._show_elapsed_time = True

    def _get_elapsed_seconds(self) -> float:
        """Get current elapsed time in seconds."""
        if self._recording_start_time is not None:
            return self._elapsed_seconds + (time.time() - self._recording_start_time)
        return self._elapsed_seconds

    def _format_elapsed_time(self, seconds: float) -> str:
        """Format seconds as MM:SS."""
        minutes = int(seconds) // 60
        secs = int(seconds) % 60
        return f"{minutes:02d}:{secs:02d}"

    def _draw_elapsed_time(self, cr: cairo.Context, width: int, height: int, chip_height: int = 36):
        """Draw elapsed time centered below the chip."""
        if not self._show_elapsed_time:
            return

        elapsed = self._get_elapsed_seconds()
        text = self._format_elapsed_time(elapsed)

        # Timer dimensions
        timer_width = 80
        timer_height = 20
        gap = 6
        timer_x = (width - timer_width) / 2.0
        timer_y = chip_height + gap
        timer_radius = timer_height / 2.0

        # Draw timer background
        bg_color = theme.background
        if len(bg_color) == 3:
            bg_rgba = (*bg_color, 0.9)
        else:
            bg_rgba = bg_color
        cr.set_source_rgba(*bg_rgba)
        self._rounded_rectangle(cr, timer_x, timer_y, timer_width, timer_height, timer_radius)
        cr.fill()

        # Draw text
        cr.select_font_face(
            "monospace",
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_NORMAL
        )
        cr.set_font_size(11)
        extents = cr.text_extents(text)
        text_width = extents.width
        text_height = extents.height
        text_x = timer_x + (timer_width - text_width) / 2.0
        text_y = timer_y + (timer_height + text_height) / 2.0 - 2
        bar_color = theme.bar_right
        cr.set_source_rgba(bar_color[0], bar_color[1], bar_color[2], 0.95)
        cr.move_to(text_x, text_y)
        cr.show_text(text)

    def _rounded_rectangle(self, cr, x, y, width, height, radius):
        """Draw a rounded rectangle path with clipped radius."""
        if width < 2 * radius:
            radius = width / 2.0
        if height < 2 * radius:
            radius = height / 2.0
        cr.new_path()
        cr.arc(x + radius, y + radius, radius, math.pi, 1.5 * math.pi)
        cr.arc(x + width - radius, y + radius, radius, 1.5 * math.pi, 2 * math.pi)
        cr.arc(x + width - radius, y + height - radius, radius, 0, 0.5 * math.pi)
        cr.arc(x + radius, y + height - radius, radius, 0.5 * math.pi, math.pi)
        cr.close_path()
