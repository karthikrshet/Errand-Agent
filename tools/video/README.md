# Demo film pipeline

`make_video.py` produces [`docs/demo/errand-agent-demo.mp4`](../../docs/demo/errand-agent-demo.mp4)
(plus the README GIF and poster). Every app scene is the **real web app** in keyless demo mode
with a fresh database and a forced mock-world scenario, driven by Playwright with an animated
cursor. Nothing is mocked up.

| File | Role |
|---|---|
| `script.py` | Scenes and narration: one caption per sentence, plus the on-screen actions for each |
| `deck.html` | Animated title, architecture, guarantees, evals and outro slides (they build per sentence) |
| `overlay.js` | Injected cursor, click ripple, captions, scene chip, row highlights |
| `make_video.py` | Narration (Kokoro neural voice, local), recording (Edge), assembly (ffmpeg) |

Each sentence's caption, audio offset and actions start together, so voice, captions and
on-screen events stay in sync even when the app responds slowly.

## Regenerate

You need Microsoft Edge, and a separate tool environment so the project's own dependencies stay
clean. The narration uses [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), an open-source
(Apache-2.0) neural text-to-speech model that runs locally, so no narration text leaves the machine.

```bash
python -m venv .videnv
.videnv/Scripts/python -m pip install playwright imageio-ffmpeg kokoro-onnx soundfile
mkdir kokoro && cd kokoro
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
cd ..
KOKORO_DIR=kokoro VIDEO_PYTHON=.videnv/Scripts/python make video
```

Environment options:
- `VIDEO_VOICE` sets the Kokoro voice (default `af_heart`; try `am_michael` or `bm_george`).
- `VIDEO_SPEED` sets the narration pace.
- `VIDEO_TTS=sapi` uses the built-in Windows voice instead (no downloads, lower quality).
- `VIDEO_ONLY=booking,evals` renders only the listed scenes.
- `VIDEO_WORK=<dir>` together with `VIDEO_REDO=arch` re-records only the listed scenes and reuses the others.
