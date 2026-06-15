"""
mic-osd - A minimal audio visualization OSD for Wayland/Hyprland.

Shows a real-time microphone input visualization overlay.
Supports two modes:
- Standalone: runs until killed (SIGTERM/SIGINT)
- Daemon: stays running, shows on SIGUSR1, hides on SIGUSR2
"""

import sys
import signal
import os
import time
import json
import numpy as np
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib


def is_gnome():
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
    return 'gnome' in desktop


def _resolve_input_device():
    """Find the audio input device configured in hyprwhspr config."""
    try:
        config_path = Path.home() / '.config' / 'hyprwhspr' / 'config.json'
        if not config_path.exists():
            return None
        with open(config_path, 'r') as f:
            config = json.load(f)
        device_name = config.get('audio_device_name')
        if not device_name:
            return None
        import sounddevice as sd
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0 and device_name.lower() in device['name'].lower():
                return i
        # Fallback: pulse device
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0 and 'pulse' in device['name'].lower():
                return i
        return None
    except Exception:
        return None

from .window import OSDWindow, load_css
from .audio import AudioMonitor
from .visualizations import VISUALIZATIONS
from .theme import ThemeWatcher

# Import paths with fallback for daemon context
try:
    from ..src.paths import RECORDING_STATUS_FILE, VISUALIZER_STATE_FILE, AUDIO_LEVEL_FILE
except ImportError:
    try:
        from src.paths import RECORDING_STATUS_FILE, VISUALIZER_STATE_FILE, AUDIO_LEVEL_FILE
    except ImportError:
        # Fallback: construct paths manually if imports fail
        home = Path.home()
        xdg_config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        RECORDING_STATUS_FILE = xdg_config / 'hyprwhspr' / 'recording_status'
        VISUALIZER_STATE_FILE = xdg_config / 'hyprwhspr' / 'visualizer_state'
        AUDIO_LEVEL_FILE = xdg_config / 'hyprwhspr' / 'audio_level'


class MicOSD:
    """
    Mic-osd application with show/hide support.
    """
    
    def __init__(self, visualization="waveform", width=140, height=62, daemon=False):
        self.main_loop = None
        self.app = None
        self.audio_monitor = None
        self.window = None
        self.update_timer_id = None
        self._auto_hide_timeout_id = None
        self._state_poll_timer_id = None
        self._recording_status_poll_timer_id = None
        self._last_visualizer_state = None
        self.daemon = daemon
        self.visible = False
        self.theme_watcher = None
        self._should_stop = False

        # Fallback state when audio monitor fails (device conflict)
        self._audio_monitor_failed = False
        self._file_based_level = 0.0

        # Grace counter for recording-status poll (avoid premature hide)
        self._recording_status_missing_count = 0

        print('[MIC-OSD] Daemon initialized', flush=True)

        # Get visualization
        viz_class = VISUALIZATIONS.get(visualization, VISUALIZATIONS["waveform"])
        self.visualization = viz_class()
        self.width = width
        self.height = height

    def run(self):
        """Start the OSD and run until killed."""
        if is_gnome():
            self._run_with_gtk_application()
        else:
            self._run_with_main_loop()

    def _run_with_gtk_application(self):
        self.app = Gtk.Application(application_id="com.hyprwhspr.mic-osd")
        self.app.connect('activate', self._gtk_on_activate)
        self.app.connect('shutdown', lambda _: self._cleanup())
        # Check if stop was requested before running (unlikely but possible)
        if self._should_stop:
            self._cleanup()
            return
        try:
            self.app.run(None)
        except KeyboardInterrupt:
            pass
        finally:
            # Ensure cleanup happens even if exception occurs
            # (shutdown signal may not be emitted on exception)
            self._cleanup()

    def _gtk_on_activate(self, app):
        # Clean up existing resources if activation happens multiple times
        if self.window:
            # Stop timers and audio monitoring before removing window
            if self.update_timer_id:
                GLib.source_remove(self.update_timer_id)
                self.update_timer_id = None
            if self._state_poll_timer_id:
                GLib.source_remove(self._state_poll_timer_id)
                self._state_poll_timer_id = None
            if self._recording_status_poll_timer_id:
                GLib.source_remove(self._recording_status_poll_timer_id)
                self._recording_status_poll_timer_id = None
            if self._auto_hide_timeout_id:
                GLib.source_remove(self._auto_hide_timeout_id)
                self._auto_hide_timeout_id = None
            if self.audio_monitor:
                self.audio_monitor.stop()
                self.audio_monitor = None
            app.remove_window(self.window)
            self.window = None
        
        if self.theme_watcher:
            self.theme_watcher.stop()
            self.theme_watcher = None
        
        load_css()

        self.window = OSDWindow(self.visualization, self.width, self.height)
        app.add_window(self.window)

        self.theme_watcher = ThemeWatcher(on_theme_changed=self._on_theme_changed)
        self.theme_watcher.start()

        self._initial_visibility()

    def _run_with_main_loop(self):
        # Initialize GTK
        Gtk.init()

        # Load CSS
        load_css()

        # Create window (hidden in daemon mode)
        self.window = OSDWindow(self.visualization, self.width, self.height)

        # Start theme watcher for live theme updates
        self.theme_watcher = ThemeWatcher(on_theme_changed=self._on_theme_changed)
        self.theme_watcher.start()

        self._initial_visibility()

        # Check if stop was requested before main loop was created
        if self._should_stop:
            return

        # Create main loop
        self.main_loop = GLib.MainLoop()

        try:
            self.main_loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

    def _initial_visibility(self):
        # If a signal handler already set visibility before window creation,
        # respect that state (handles race condition with early signals)
        if self.visible:
            # Signal handler wants to show it
            self._show()
        elif self.daemon:
            # Start hidden, wait for SIGUSR1
            self.window.set_visible(False)
        else:
            # Show immediately
            self._show()

    def _show(self):
        """Show the OSD and start audio monitoring."""
        # If already visible and audio monitoring is running, return early
        # This handles the normal case where _show() is called multiple times
        if self.visible and self.audio_monitor and self.update_timer_id:
            return

        # If window doesn't exist yet (race condition with signal handlers),
        # just set the visible flag and return. The window will be shown when
        # it's created in _gtk_on_activate().
        if not self.window:
            self.visible = True
            return

        self.visible = True
        self.window.set_visible(True)

        # Start audio monitoring
        if not self.audio_monitor:
            self.audio_monitor = AudioMonitor(samplerate=16000, blocksize=1024)

        try:
            device_id = _resolve_input_device()
            self.audio_monitor.start(device=device_id)
            self._audio_monitor_failed = False
            print(f"[MIC-OSD] Audio monitor started on device {device_id}", flush=True)
        except Exception as e:
            # Audio monitoring failed (e.g., device busy). Keep the window open
            # and fall back to reading the audio level from the main process.
            print(f"[MIC-OSD] Failed to start audio monitoring: {e} - using file-based level fallback", flush=True)
            self._audio_monitor_failed = True

        # Start update timer immediately for fast opening (60 FPS)
        if not self.update_timer_id:
            self.update_timer_id = GLib.timeout_add(16, self._update)

        # Start state file polling (100ms interval)
        if not self._state_poll_timer_id:
            self._state_poll_timer_id = GLib.timeout_add(100, self._poll_state_file)

        # Start recording status polling (100ms interval)
        if not self._recording_status_poll_timer_id:
            self._recording_status_poll_timer_id = GLib.timeout_add(100, self._poll_recording_status)

        # Start auto-hide timeout (15 seconds)
        if self._auto_hide_timeout_id:
            GLib.source_remove(self._auto_hide_timeout_id)
        self._auto_hide_timeout_id = GLib.timeout_add_seconds(15, self._auto_hide_callback)

        # Always reset the visualizer to recording state when shown so the timer
        # and any per-recording animation state starts fresh
        if self.visualization and hasattr(self.visualization, 'set_state'):
            try:
                self.visualization.set_state('recording')
            except Exception:
                pass
    
    def _hide(self):
        """Hide the OSD and stop audio monitoring."""
        if not self.visible:
            return
        
        # If window doesn't exist yet (race condition with signal handlers),
        # just set the visible flag and return.
        if not self.window:
            self.visible = False
            return
        
        try:
            self.visible = False
            self.window.set_visible(False)
            
            # Stop update timer
            if self.update_timer_id:
                GLib.source_remove(self.update_timer_id)
                self.update_timer_id = None

            # Stop state polling timer
            if self._state_poll_timer_id:
                GLib.source_remove(self._state_poll_timer_id)
                self._state_poll_timer_id = None

            # Stop recording status polling timer
            if self._recording_status_poll_timer_id:
                GLib.source_remove(self._recording_status_poll_timer_id)
                self._recording_status_poll_timer_id = None

            # Cancel auto-hide timeout
            if self._auto_hide_timeout_id:
                GLib.source_remove(self._auto_hide_timeout_id)
                self._auto_hide_timeout_id = None
            
            # Stop audio monitoring
            if self.audio_monitor:
                self.audio_monitor.stop()
                self.audio_monitor = None
            
            # Reset fallback state
            self._audio_monitor_failed = False
            self._file_based_level = 0.0
            self._recording_status_missing_count = 0
        except Exception as e:
            # Ensure window is hidden even if exceptions occur
            print(f"[MIC-OSD] Error in _hide(): {e}", flush=True)
            self.visible = False
            if self.window:
                try:
                    self.window.set_visible(False)
                except Exception:
                    pass
            # Clean up timers on error
            if self.update_timer_id:
                try:
                    GLib.source_remove(self.update_timer_id)
                except Exception:
                    pass
                self.update_timer_id = None
            if self._state_poll_timer_id:
                try:
                    GLib.source_remove(self._state_poll_timer_id)
                except Exception:
                    pass
                self._state_poll_timer_id = None
            if self._recording_status_poll_timer_id:
                try:
                    GLib.source_remove(self._recording_status_poll_timer_id)
                except Exception:
                    pass
                self._recording_status_poll_timer_id = None
            if self._auto_hide_timeout_id:
                try:
                    GLib.source_remove(self._auto_hide_timeout_id)
                except Exception:
                    pass
                self._auto_hide_timeout_id = None
            # Clean up audio monitor on error
            if self.audio_monitor:
                try:
                    self.audio_monitor.stop()
                except Exception:
                    pass
                self.audio_monitor = None
            
            # Reset fallback state
            self._audio_monitor_failed = False
            self._file_based_level = 0.0
            self._recording_status_missing_count = 0
    
    def _update(self):
        """Update visualization with current audio data."""
        if not self.window or not self.visible:
            return True  # Continue timer

        if self.audio_monitor and not self._audio_monitor_failed:
            level = self.audio_monitor.get_level()
            samples = self.audio_monitor.get_samples()
        else:
            # Fallback: read level from main process's audio_level file
            try:
                if AUDIO_LEVEL_FILE.exists():
                    level = float(AUDIO_LEVEL_FILE.read_text().strip())
                else:
                    level = 0.0
            except Exception:
                level = 0.0
            self._file_based_level = level
            # Generate synthetic waveform samples from the level
            t = np.linspace(0, 2 * np.pi * 4, 1024)
            samples = level * np.sin(t) * 0.5 + (np.random.random(1024) - 0.5) * 0.1 * level

        self.window.update(level, samples)

        # Debug level log once per second (~60 frames)
        if not hasattr(self, '_update_counter'):
            self._update_counter = 0
        self._update_counter += 1
        if self._update_counter % 60 == 0:
            samples_max = float(np.max(np.abs(samples))) if samples is not None and len(samples) > 0 else 0.0
            bars_max = float(np.max(self.visualization.bar_heights)) if hasattr(self.visualization, 'bar_heights') else -1.0
            print(f'[MIC-OSD] level={level:.4f} samples_max={samples_max:.4f} bars_max={bars_max:.4f} fallback={self._audio_monitor_failed}', flush=True)
            try:
                with open('/tmp/mic-osd-level.txt', 'w') as f:
                    f.write(f'{level:.4f} {samples_max:.4f} {bars_max:.4f}\n')
            except Exception:
                pass
        return True  # Continue timer

    def _poll_state_file(self):
        """Poll the visualizer state file and update visualization state."""
        try:
            if VISUALIZER_STATE_FILE.exists():
                with open(VISUALIZER_STATE_FILE, 'r') as f:
                    state = f.read().strip()
                    if state and state != self._last_visualizer_state:
                        self._last_visualizer_state = state
                        # Update visualization state if it has the set_state method
                        if hasattr(self.visualization, 'set_state'):
                            self.visualization.set_state(state)
            else:
                # No state file means default to recording state
                if self._last_visualizer_state != 'recording':
                    self._last_visualizer_state = 'recording'
                    if hasattr(self.visualization, 'set_state'):
                        self.visualization.set_state('recording')
        except Exception:
            pass  # Ignore file read errors
        return True  # Continue polling

    def _poll_recording_status(self):
        """Poll the recording status file and auto-hide if recording is no longer active."""
        if not self.visible:
            return False  # Stop polling when not visible

        try:
            missing_or_stale = False
            if not RECORDING_STATUS_FILE.exists():
                missing_or_stale = True
            else:
                status = RECORDING_STATUS_FILE.read_text().strip()
                file_age = time.time() - RECORDING_STATUS_FILE.stat().st_mtime
                if status != 'true' or file_age > 60.0:
                    missing_or_stale = True

            if missing_or_stale:
                self._recording_status_missing_count += 1
            else:
                self._recording_status_missing_count = 0

            # Only hide after 2 seconds of consecutive missing/stale checks
            if self._recording_status_missing_count >= 20:
                print("[MIC-OSD] Recording status missing/stale for 2s - auto-hiding", flush=True)
                self._hide()
                return False  # Stop polling
        except Exception:
            # File read error - increment counter but don't hide immediately
            self._recording_status_missing_count += 1
            if self._recording_status_missing_count >= 20:
                try:
                    self._hide()
                except Exception:
                    pass
                return False

        return True  # Continue polling

    def _auto_hide_callback(self):
        """Auto-hide callback triggered after 15 seconds of visibility."""
        if not self.visible:
            self._auto_hide_timeout_id = None
            return False  # Don't repeat
        
        # Check if recording is still active and fresh before hiding
        # This prevents hiding during normal long recordings
        recording_active = False
        try:
            if RECORDING_STATUS_FILE.exists():
                with open(RECORDING_STATUS_FILE, 'r') as f:
                    status = f.read().strip()
                    file_age = time.time() - RECORDING_STATUS_FILE.stat().st_mtime
                    if status == 'true' and file_age < 60.0:
                        recording_active = True
        except Exception:
            # File read error - assume not recording, allow hide
            pass
        
        if recording_active:
            # Recording is still active - reset timeout instead of hiding
            print("[MIC-OSD] Recording active - resetting auto-hide timeout", flush=True)
            self._auto_hide_timeout_id = GLib.timeout_add_seconds(15, self._auto_hide_callback)
            return False  # Don't repeat (new timeout already set)
        else:
            # Recording not active or stale - window is stuck, hide it
            print("[MIC-OSD] Auto-hiding window after timeout (recording not active)", flush=True)
            self._hide()
            self._auto_hide_timeout_id = None
            return False  # Don't repeat
    
    def _on_theme_changed(self):
        """Called when the Omarchy theme changes."""
        # Force a redraw to pick up new colors
        if self.window:
            self.window.drawing_area.queue_draw()
    
    def stop(self):
        """Stop the OSD completely."""
        if self.app:
            self.app.quit()
        elif self.main_loop:
            self.main_loop.quit()
        else:
            # Neither app nor main_loop exists yet (early stop request)
            # Set flag and call cleanup directly
            self._should_stop = True
            self._cleanup()
    
    def _cleanup(self):
        """Clean up resources."""
        if self.update_timer_id:
            GLib.source_remove(self.update_timer_id)
            self.update_timer_id = None

        if self._state_poll_timer_id:
            GLib.source_remove(self._state_poll_timer_id)
            self._state_poll_timer_id = None

        if self._recording_status_poll_timer_id:
            GLib.source_remove(self._recording_status_poll_timer_id)
            self._recording_status_poll_timer_id = None

        if self._auto_hide_timeout_id:
            GLib.source_remove(self._auto_hide_timeout_id)
            self._auto_hide_timeout_id = None

        if self.audio_monitor:
            self.audio_monitor.stop()
            self.audio_monitor = None

        if self.window:
            if self.app:
                self.app.remove_window(self.window)
            self.window = None

        if self.theme_watcher:
            self.theme_watcher.stop()
            self.theme_watcher = None


# Global instance for signal handlers
_app = None


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT - quit."""
    if _app:
        _app.stop()


def _sigusr1_handler(signum, frame):
    """Handle SIGUSR1 - show OSD."""
    if _app:
        GLib.idle_add(_app._show)


def _sigusr2_handler(signum, frame):
    """Handle SIGUSR2 - hide OSD."""
    if _app:
        GLib.idle_add(_app._hide)


def main():
    """Entry point."""
    global _app
    
    # Ensure stdout is line-buffered so logs are captured even when not a TTY
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    
    import argparse
    parser = argparse.ArgumentParser(
        prog="mic-osd",
        description="Show microphone input visualization overlay"
    )
    parser.add_argument(
        "-v", "--viz",
        choices=["waveform", "vu_meter"],
        default="waveform",
        help="Visualization type (default: waveform)"
    )
    parser.add_argument(
        "-w", "--width",
        type=int,
        default=140,
        help="Window width (default: 140)"
    )
    parser.add_argument(
        "-H", "--height",
        type=int,
        default=62,
        help="Window height (default: 62)"
    )
    parser.add_argument(
        "-d", "--daemon",
        action="store_true",
        help="Run as daemon (start hidden, show on SIGUSR1, hide on SIGUSR2)"
    )
    args = parser.parse_args()
    
    # Set up signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGUSR1, _sigusr1_handler)
    signal.signal(signal.SIGUSR2, _sigusr2_handler)
    
    # Run
    _app = MicOSD(
        visualization=args.viz,
        width=args.width,
        height=args.height,
        daemon=args.daemon
    )
    _app.run()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
