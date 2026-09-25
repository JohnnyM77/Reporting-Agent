#!/usr/bin/env python3
"""Render a sample Ned transcript digest PDF from the test fixture.

    python scripts/ned_sample_transcript_pdf.py                 # auto backend
    python scripts/ned_sample_transcript_pdf.py --backend playwright

Writes outputs/sample_transcript_digest.pdf (outputs/ is gitignored) so the
layout can be eyeballed without an API key or a live run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ned import transcript_pdf as pdfmod  # noqa: E402
from ned.transcript_digest import digest_json_to_markdown, normalise_digest  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ned_transcript_digest_sample.json"

_SAMPLE_LINES = [
    "In the autumn of 1873 Jay Cooke was the most famous banker in America.",
    "He had sold the Union's war bonds to ordinary savers, and he meant to do the same for a railway.",
    "The Northern Pacific would run from Lake Superior to the Pacific, through country with almost no one in it.",
    "The bonds paid seven point three per cent, and the pamphlets promised orange groves in Minnesota.",
    "Track went down ahead of any traffic that could pay for it.",
    "Then in May the Vienna stock exchange crashed, and European buyers stopped taking American railway paper.",
    "By September the bonds had stopped selling altogether.",
    "On the eighteenth, Jay Cooke and Company closed its doors.",
    "Two days later the New York Stock Exchange shut for ten days, the first time it had ever done so.",
    "A quarter of the country's railways would default over the next few years.",
]


def _sample_segments(minutes: int = 54) -> list[dict]:
    segs = []
    t = 0.0
    i = 0
    while t < minutes * 60:
        text = _SAMPLE_LINES[i % len(_SAMPLE_LINES)]
        segs.append({"text": text, "start": t, "duration": 7.5})
        t += 7.5
        i += 1
    return segs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=("auto", "weasyprint", "playwright"), default="auto")
    ap.add_argument("--out", default=str(REPO_ROOT / "outputs" / "sample_transcript_digest.pdf"))
    args = ap.parse_args(argv)

    digest = normalise_digest(json.loads(FIXTURE.read_text(encoding="utf-8")))
    segments = _sample_segments()
    out = Path(args.out)

    saved = {}
    if args.backend != "auto":
        # Force one backend by making the other one unavailable for this call.
        other = "playwright" if args.backend == "weasyprint" else "weasyprint"
        attr = f"_render_with_{other}"

        def _unavailable(html, path):
            raise ImportError(f"{other} disabled for this sample run")

        saved[attr] = getattr(pdfmod, attr)
        setattr(pdfmod, attr, _unavailable)

    try:
        pdfmod.build_transcript_pdf(
            kind="podcast",
            title="Hitting the Buffers: The 1873 railway bust that broke one of America's greatest financiers",
            source_url="https://podcasts.apple.com/sg/podcast/hitting-the-buffers-the-1873-railway-bust-that-broke/id1376303362?i=1000764231178",
            digest_markdown=digest_json_to_markdown(digest),
            transcript_text=" ".join(s["text"] for s in segments),
            output_path=out,
            timestamp=dt.datetime(2026, 9, 25, 13, 27),
            digest_json=digest,
            channel="The Story of Money",
            provenance="Whisper transcription",
            segments=segments,
        )
    finally:
        for attr, fn in saved.items():
            setattr(pdfmod, attr, fn)
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
