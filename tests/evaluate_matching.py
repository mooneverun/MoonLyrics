"""Run fixed real-song cohorts through automatic matching; save metadata, never lyric text."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from moonlyrics import __version__
from moonlyrics.lyrics import LyricsFinder, Track
from moonlyrics.lyrics_worker import LyricsWorker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("history", "holdout", "fresh"))
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    fixtures = Path(__file__).parent / "fixtures"
    if args.group in ("holdout", "fresh"):
        manifest = json.loads((fixtures / "matching_holdout.json").read_text(encoding="utf-8"))
        cases = manifest["fresh_cases" if args.group == "fresh" else "cases"]
    else:
        cases = json.loads((fixtures / "matching_cases.json").read_text(encoding="utf-8"))["cases"]
        cases = [dict(input=case["input"], negative="Instrumental" in case["input"]["title"]) for case in cases]
        cases.extend([dict(input=dict(title="蓝本", artist="Capper", album="蓝本 - Single", duration=171.135)),
                      dict(input=dict(title="Eva&Lov3", artist="Capper & kkluv", album="Gun-fire Angel", duration=183.621))])
    order = ("netease", "kugou", "qq", "lrclib")
    report = dict(version=__version__, group=args.group, source_order=order,
                  utc=datetime.now(timezone.utc).isoformat(), review="Metadata review pending; not a listening test", cases=[])
    limits, lrclib_limit = {}, 0.
    for case in cases:
        if report["cases"]:
            time.sleep(1.)
        finder = LyricsFinder()
        finder.endpoint_retry_at.update(limits)
        finder.lrclib_retry_at = lrclib_limit
        target = Track(**{key: value for key, value in case["input"].items() if key != "title_aliases"})
        started = time.monotonic()
        row = dict(input=case["input"], apple_id=case.get("apple_id"), negative=case.get("negative", False), attempts=[])
        try:
            for delay in (0.,) + LyricsWorker.retry_delays:
                if delay:
                    time.sleep(delay)
                document, message = finder.find(target, source_order=order)
                row["attempts"].append(dict(ok=document is not None, message=message, diagnostics=finder.diagnostics))
                if document is not None or not finder.retryable:
                    break
            row.update(ok=document is not None, source=document.source if document else None,
                       title=document.title if document else None, artist=document.artist if document else None,
                       lines=len(document.lines) if document else 0, elapsed=round(time.monotonic() - started, 3))
        finally:
            limits.update(finder.endpoint_retry_at)
            lrclib_limit = finder.lrclib_retry_at
            finder.close()
        report["cases"].append(row)
        positives = [item for item in report["cases"] if not item["negative"]]
        report["summary"] = dict(tested=len(positives), first_attempt_found=sum(item["attempts"][0]["ok"] for item in positives),
                                 automatic_found=sum(item["ok"] for item in positives),
                                 median_seconds=round(statistics.median(item["elapsed"] for item in positives), 3),
                                 negatives_rejected=sum(not item["ok"] for item in report["cases"] if item["negative"]))
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: row[key] for key in ("input", "ok", "source", "title", "lines", "elapsed")}, ensure_ascii=False), flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return int(any(item["ok"] for item in report["cases"] if item["negative"]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
