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
| `make_video.py` | Narration (Windows speech, offline), recording (Edge), assembly (ffmpeg) |

Each sentence's caption, audio offset and actions start together, so voice, captions and
on-screen events stay in sync even when the app responds slowly.

## Regenerate

You need Windows (for the built-in voices), Microsoft Edge, and a separate tool environment,
so the project's own dependencies stay clean:

```bash
python -m venv .videnv && .videnv/Scripts/python -m pip install playwright imageio-ffmpeg
VIDEO_PYTHON=.videnv/Scripts/python make video
```

Environment options:
- `VIDEO_VOICE` sets the narration voice (default "Microsoft David Desktop").
- `VIDEO_ONLY=booking,evals` renders only the listed scenes.
- `VIDEO_WORK=<dir>` together with `VIDEO_REDO=arch` re-records only the listed scenes and reuses the others.
