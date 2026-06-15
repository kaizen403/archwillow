"""
ElevenLabs Scribe v2 Realtime WebSocket client for hyprwhspr
Implements the streaming speech-to-text protocol at:
    wss://api.elevenlabs.io/v1/speech-to-text/realtime
"""

import sys
import json
import base64
import threading
import time
from typing import Optional, List
from queue import Queue, Empty

try:
    import numpy as np
except (ImportError, ModuleNotFoundError) as e:
    print("ERROR: python-numpy is not available in this Python environment.", file=sys.stderr)
    print(f"ImportError: {e}", file=sys.stderr)
    sys.exit(1)

try:
    import websocket
except (ImportError, ModuleNotFoundError) as e:
    print("ERROR: websocket-client is not available in this Python environment.", file=sys.stderr)
    print(f"ImportError: {e}", file=sys.stderr)
    print("This is a required dependency. Please install it:", file=sys.stderr)
    print("  pip install websocket-client>=1.6.0", file=sys.stderr)
    sys.exit(1)


class ElevenLabsRealtimeClient:
    """WebSocket client for ElevenLabs Scribe v2 Realtime API"""

    REGION_URLS = {
        'default': 'wss://api.elevenlabs.io/v1/speech-to-text/realtime',
        'us': 'wss://api.us.elevenlabs.io/v1/speech-to-text/realtime',
        'eu': 'wss://api.eu.residency.elevenlabs.io/v1/speech-to-text/realtime',
        'in': 'wss://api.in.residency.elevenlabs.io/v1/speech-to-text/realtime',
        'sg': 'wss://api.sg.residency.elevenlabs.io/v1/speech-to-text/realtime',
    }

    def __init__(self):
        self.ws = None
        self.url = None
        self.api_key = None
        self.model = None

        # Configurable session options
        self.audio_format = 'pcm_16000'
        self.commit_strategy = 'vad'
        self.language = None
        self.no_verbatim = True
        self.include_timestamps = False
        self.include_language_detection = False
        self.region = 'default'
        self.vad_silence_threshold_secs = 1.5
        self.vad_threshold = 0.4
        self.min_speech_duration_ms = 100
        self.min_silence_duration_ms = 100
        self.enable_logging = True
        self.keyterms: List[str] = []

        # Connection state
        self.connected = False
        self.connecting = False
        self.session_started = False
        self.receiver_thread = None
        self.receiver_running = False
        self._intentional_close = False

        # Reconnection
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delays = [1, 2, 4, 8, 16]

        # Event handling
        self.event_queue = Queue()
        self.lock = threading.Lock()

        # Transcription state
        self.partial_text = ''
        self.current_response_text = ''
        self.response_complete = False
        self.response_event = threading.Event()
        self.session_event = threading.Event()

        # Audio sampling
        self.sample_rate = 16000

        # Pre-connection audio buffer (audio captured before WebSocket is ready)
        self._audio_buffer: List[bytes] = []

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self, url: str, api_key: str, model: str) -> bool:
        """Establish WebSocket connection and wait for session_started."""
        self.url = url
        self.api_key = api_key
        self.model = model
        return self._connect_internal()

    def _connect_internal(self) -> bool:
        if self.connecting:
            return False

        self.connecting = True

        try:
            headers = {
                'xi-api-key': self.api_key,
            }

            print(f'[ELEVENLABS REALTIME] Connecting to {self.url}...', flush=True)

            self.ws = websocket.WebSocketApp(
                self.url,
                header=[f'{k}: {v}' for k, v in headers.items()],
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )

            ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
            ws_thread.start()

            # Wait for TCP/WebSocket connection
            timeout = 10.0
            start_time = time.monotonic()
            while not self.connected and (time.monotonic() - start_time) < timeout:
                time.sleep(0.05)

            if not self.connected:
                print('[ELEVENLABS REALTIME] Connection timeout', flush=True)
                return False

            # Wait for the server session_started event
            session_timeout = 5.0
            start_time = time.monotonic()
            while not self.session_started and (time.monotonic() - start_time) < session_timeout:
                time.sleep(0.05)

            if self.session_started:
                self.reconnect_attempts = 0
                print('[ELEVENLABS REALTIME] Connected and session started', flush=True)
                return True

            print('[ELEVENLABS REALTIME] Connected but session did not start', flush=True)
            return False

        except Exception as e:
            print(f'[ELEVENLABS REALTIME] Connection error: {e}', flush=True)
            return False
        finally:
            self.connecting = False

    def _on_open(self, _ws):
        with self.lock:
            self.connected = True
            self.connecting = False

        if not self.receiver_running:
            self.receiver_running = True
            self.receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
            self.receiver_thread.start()

    def _on_message(self, _ws, message):
        try:
            event = json.loads(message)
            self.event_queue.put(event)
        except json.JSONDecodeError as e:
            print(f'[ELEVENLABS REALTIME] Failed to parse event: {e}', flush=True)

    def _on_error(self, _ws, error):
        print(f'[ELEVENLABS REALTIME] WebSocket error: {error}', flush=True)

    def _on_close(self, _ws, close_status_code, _close_msg):
        with self.lock:
            self.connected = False
            self.session_started = False
        print(f'[ELEVENLABS REALTIME] WebSocket closed (code: {close_status_code})', flush=True)

        # Do not auto-reconnect on normal idle closes (code 1000).
        # ElevenLabs drops idle sessions quickly; we reconnect on-demand when
        # recording starts. Only auto-reconnect on abnormal closes.
        if not self._intentional_close and close_status_code not in (1000, None):
            self._attempt_reconnect()

    # ------------------------------------------------------------------
    # Receiver thread
    # ------------------------------------------------------------------
    def _receiver_loop(self):
        while self.receiver_running:
            try:
                event = self.event_queue.get(timeout=0.1)
                self._handle_event(event)
            except Empty:
                continue
            except Exception as e:
                print(f'[ELEVENLABS REALTIME] Error in receiver loop: {e}', flush=True)

    def _handle_event(self, event: dict):
        msg_type = event.get('message_type', '')

        if msg_type == 'session_started':
            session_id = event.get('session_id', '')
            print(f'[ELEVENLABS REALTIME] Session started: {session_id}', flush=True)
            with self.lock:
                self.session_started = True
            self.session_event.set()
            return

        if msg_type == 'partial_transcript':
            text = event.get('text', '')
            with self.lock:
                self.partial_text = text
            print(f'[ELEVENLABS REALTIME] Partial: {text}', flush=True)
            return

        if msg_type in ('committed_transcript', 'committed_transcript_with_timestamps'):
            text = event.get('text', '').strip()
            if text:
                with self.lock:
                    if self.current_response_text:
                        self.current_response_text += ' ' + text
                    else:
                        self.current_response_text = text
                    self.response_complete = True
                self.response_event.set()
                print(f'[ELEVENLABS REALTIME] Committed ({len(text)} chars)', flush=True)
            return

        # Error family
        if msg_type in (
            'error', 'auth_error', 'quota_exceeded', 'commit_throttled',
            'unaccepted_terms', 'rate_limited', 'queue_overflow',
            'resource_exhausted', 'session_time_limit_exceeded', 'input_error',
            'chunk_size_exceeded', 'insufficient_audio_activity', 'transcriber_error'
        ):
            error = event.get('error', 'Unknown error')
            print(f'[ELEVENLABS REALTIME] {msg_type}: {error}', flush=True)
            with self.lock:
                self.response_complete = True
            self.response_event.set()
            return

    # ------------------------------------------------------------------
    # Audio streaming
    # ------------------------------------------------------------------
    def _float32_to_pcm16(self, audio_data: np.ndarray) -> bytes:
        """Convert float32 numpy array [-1, 1] to little-endian PCM16 bytes."""
        audio_clipped = np.clip(audio_data, -1.0, 1.0)
        audio_int16 = (audio_clipped * 32767).astype(np.int16)
        return audio_int16.tobytes()

    def append_audio(self, audio_chunk: np.ndarray):
        """Stream one audio chunk to ElevenLabs. Audio must be 16kHz float32 mono."""
        try:
            pcm_bytes = self._float32_to_pcm16(audio_chunk)
        except Exception as e:
            print(f'[ELEVENLABS REALTIME] Failed to convert audio: {e}', flush=True)
            return

        with self.lock:
            if not self.connected or not self.ws:
                # Buffer audio while not connected (e.g., during background reconnect)
                self._audio_buffer.append(pcm_bytes)
                # Limit buffer to ~6.4s of audio (100 chunks of 1024 samples at 16kHz)
                while len(self._audio_buffer) > 100:
                    self._audio_buffer.pop(0)
                return

            # Connected: flush any buffered audio, then send current
            buffered = self._audio_buffer.copy()
            self._audio_buffer.clear()

        for buffered_pcm in buffered:
            self._send_input_audio_chunk(buffered_pcm, commit=False)

        self._send_input_audio_chunk(pcm_bytes, commit=False)

    def _send_input_audio_chunk(self, pcm_bytes: bytes, commit: bool = False):
        if not self.connected or not self.ws:
            return

        base64_audio = base64.b64encode(pcm_bytes).decode('utf-8')
        event = {
            'message_type': 'input_audio_chunk',
            'audio_base_64': base64_audio,
            'commit': commit,
            'sample_rate': self.sample_rate,
        }
        self.ws.send(json.dumps(event))

    def _send_final_commit(self):
        """Flush remaining audio by sending a small silence chunk with commit=True."""
        if not self.connected or not self.ws:
            return

        # 0.1s of silence as a valid PCM chunk; enough for the server to process reliably.
        silence_samples = int(self.sample_rate * 0.1)
        silence_bytes = np.zeros(silence_samples, dtype=np.int16).tobytes()
        self._send_input_audio_chunk(silence_bytes, commit=True)

    def clear_audio_buffer(self):
        """Reset transcription state before a new recording."""
        with self.lock:
            self.partial_text = ''
            self.current_response_text = ''
            self.response_complete = False
            self.response_event.clear()
            self._audio_buffer.clear()
        print('[ELEVENLABS REALTIME] Buffer cleared', flush=True)

    # ------------------------------------------------------------------
    # Commit / final transcription
    # ------------------------------------------------------------------
    def commit_and_get_text(self, timeout: float = 30.0) -> str:
        """Flush remaining audio and return the final transcript."""
        if not self.connected or not self.ws:
            print('[ELEVENLABS REALTIME] Not connected, cannot commit', flush=True)
            return ''

        # Flush any pre-connection buffered audio before sending final commit
        with self.lock:
            buffered = self._audio_buffer.copy()
            self._audio_buffer.clear()

        for buffered_pcm in buffered:
            self._send_input_audio_chunk(buffered_pcm, commit=False)

        try:
            # Reset event state before forcing the final commit
            with self.lock:
                self.response_complete = False
                self.response_event.clear()

            # Send a small silence chunk with commit=True to flush any uncommitted audio.
            # This works for both 'manual' and 'vad' strategies.
            self._send_final_commit()
            print('[ELEVENLABS REALTIME] Sent final commit', flush=True)

            # Wait for committed transcripts, returning once no new text arrives for 0.5s.
            deadline = time.monotonic() + timeout
            last_text = ''
            last_text_time = time.monotonic()

            while time.monotonic() < deadline:
                if self.response_event.wait(timeout=0.1):
                    self.response_event.clear()
                    with self.lock:
                        current_text = self.current_response_text
                    if current_text != last_text:
                        last_text = current_text
                        last_text_time = time.monotonic()

                # If we have text and nothing new arrived for 0.05s, return it
                if last_text and (time.monotonic() - last_text_time) > 0.05:
                    with self.lock:
                        result = self.current_response_text.strip()
                        self.current_response_text = ''
                        self.response_complete = False
                        return result

            print(f'[ELEVENLABS REALTIME] Timeout waiting for committed transcript ({timeout}s)', flush=True)
            with self.lock:
                result = self.current_response_text.strip()
                self.current_response_text = ''
                self.response_complete = False
                return result

        except Exception as e:
            print(f'[ELEVENLABS REALTIME] Error in commit_and_get_text: {e}', flush=True)
            return ''

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def update_language(self, language: Optional[str]):
        """Language is fixed at session start for ElevenLabs; log a warning."""
        if language:
            print(f'[ELEVENLABS REALTIME] Note: language cannot be changed dynamically. '
                  f'Reconnect with language_code={language} to use it.', flush=True)

    def _attempt_reconnect(self) -> bool:
        """Attempt to reconnect with exponential backoff."""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            print('[ELEVENLABS REALTIME] Max reconnection attempts reached', flush=True)
            return False

        delay = self.reconnect_delays[min(self.reconnect_attempts, len(self.reconnect_delays) - 1)]
        self.reconnect_attempts += 1

        print(f'[ELEVENLABS REALTIME] Reconnecting (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts}) in {delay}s...', flush=True)
        time.sleep(delay)

        if self._connect_internal():
            print('[ELEVENLABS REALTIME] Reconnected successfully', flush=True)
            return True
        return False

    def reconnect(self) -> bool:
        """Public reconnect method used when the connection is needed but lost."""
        if self.connected and self.session_started:
            return True
        if self.connecting:
            # Wait for an in-progress connection/reconnect
            timeout = 15.0
            start = time.monotonic()
            while self.connecting and (time.monotonic() - start) < timeout:
                time.sleep(0.1)
            return self.connected and self.session_started
        return self._connect_internal()

    def close(self):
        """Close the WebSocket connection and stop the receiver thread."""
        self._intentional_close = True
        self.receiver_running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.receiver_thread and self.receiver_thread.is_alive():
            self.receiver_thread.join(timeout=1.0)
        with self.lock:
            self.connected = False
            self.session_started = False
        print('[ELEVENLABS REALTIME] Connection closed', flush=True)
