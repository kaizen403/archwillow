# archwillow

Minimal English speech-to-text overlay for Linux, powered by ElevenLabs realtime transcription.

## Features

- Low-latency transcription via ElevenLabs Scribe v2
- Small, dark, chip-style microphone visualization
- Bottom-centered overlay with elapsed timer
- Hyprland / Wayland integration

## Setup

Run the installer:

```bash
./bin/hyprwhspr setup
```

Configure `~/.config/hyprwhspr/config.json` for the ElevenLabs realtime backend:

```json
{
  "transcription_backend": "realtime-ws",
  "websocket_provider": "elevenlabs",
  "websocket_model": "scribe_v2_realtime",
  "language": "en"
}
```

## Usage

Press `Super+Alt+D` to start and stop dictation. Transcribed text is pasted into the active window.

## License

MIT
