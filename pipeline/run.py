"""
run.py — CLI entrypoint for the Store Intelligence detection pipeline.

Usage examples:
  # Process a single video
  python pipeline/run.py --video data/CAM_ENTRY_01.mp4 --store STORE_BLR_002 --camera CAM_ENTRY_01

  # Process all videos in data/ directory
  python pipeline/run.py --store STORE_BLR_002

  # Process and post events to the API in real time
  python pipeline/run.py --store STORE_BLR_002 --post-to-api http://localhost:8000

  # Non-headless (shows OpenCV window)
  python pipeline/run.py --video data/CAM_ENTRY_01.mp4 --store STORE_BLR_002 --camera CAM_ENTRY_01 --show
"""

import argparse
import os
import sys
import json
from pathlib import Path

# Allow importing from pipeline/ when run from repo root
sys.path.insert(0, str(Path(__file__).parent))

from detect import DetectionPipeline
from session_manager import SessionManager
from tracker import ReIDEngine


# Mapping from common video filename patterns to camera IDs
FILENAME_TO_CAMERA_HINTS = {
    "entry": "CAM_ENTRY_01",
    "floor": "CAM_FLOOR_01",
    "billing": "CAM_BILLING_01",
    "main": "CAM_FLOOR_01",
    "counter": "CAM_BILLING_01",
    "cam1": "CAM_ENTRY_01",
    "cam2": "CAM_FLOOR_01",
    "cam3": "CAM_BILLING_01",
    "cam 1": "CAM_ENTRY_01",
    "cam 2": "CAM_FLOOR_01",
    "cam 3": "CAM_BILLING_01",
}


def guess_camera_id(video_path: str) -> str:
    """Guess camera ID from filename."""
    stem = Path(video_path).stem.lower()
    for hint, cam_id in FILENAME_TO_CAMERA_HINTS.items():
        if hint in stem:
            return cam_id
    return f"CAM_{stem.upper().replace(' ', '_')}"


def find_videos(data_dir: str) -> list[Path]:
    """Find all video files in data directory."""
    data_path = Path(data_dir)
    video_extensions = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}
    videos = []
    for ext in video_extensions:
        videos.extend(data_path.glob(f"*{ext}"))
        videos.extend(data_path.glob(f"*{ext.upper()}"))
    return sorted(set(videos))


def load_layout_for_store(store_id: str, layout_path: str) -> dict:
    """Load store layout to get entry line hints."""
    if not os.path.exists(layout_path):
        return {}
    with open(layout_path) as f:
        layout = json.load(f)
    if isinstance(layout, list):
        for store in layout:
            if store.get("store_id") == store_id:
                return store
    return layout


def main():
    parser = argparse.ArgumentParser(
        description="Store Intelligence Detection Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--video", type=str, default=None,
        help="Path to a specific video file. If omitted, all videos in --data-dir are processed.",
    )
    parser.add_argument(
        "--store", type=str, default="STORE_BLR_002",
        help="Store ID (e.g. STORE_BLR_002).",
    )
    parser.add_argument(
        "--camera", type=str, default=None,
        help="Camera ID. Auto-detected from filename if not provided.",
    )
    parser.add_argument(
        "--data-dir", type=str, default="data",
        help="Directory containing video files and store_layout.json.",
    )
    parser.add_argument(
        "--model", type=str, default="yolov8n.pt",
        help="Path to YOLOv8 model weights.",
    )
    parser.add_argument(
        "--output", type=str, default="data/events.jsonl",
        help="Path to output JSONL events file.",
    )
    parser.add_argument(
        "--post-to-api", type=str, default=None, metavar="API_URL",
        help="If set, POST each event to this API URL in real time.",
    )
    parser.add_argument(
        "--entry-line-y", type=int, default=None,
        help="Manual entry/exit line Y coordinate in pixels. Auto-detected if omitted.",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.35,
        help="Minimum detection confidence threshold (default 0.35).",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Show annotated video frames in an OpenCV window.",
    )
    parser.add_argument(
        "--layout", type=str, default=None,
        help="Path to store_layout.json. Defaults to <data-dir>/store_layout.json.",
    )

    args = parser.parse_args()

    layout_path = args.layout or os.path.join(args.data_dir, "store_layout.json")

    # Determine which videos to process
    if args.video:
        videos = [Path(args.video)]
    else:
        videos = find_videos(args.data_dir)
        if not videos:
            print(f"[ERROR] No video files found in '{args.data_dir}'.")
            print("        Place .mp4/.avi/.mov files there or use --video <path>.")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"STORE INTELLIGENCE DETECTION PIPELINE")
    print(f"  Store:    {args.store}")
    print(f"  Videos:   {len(videos)}")
    print(f"  Output:   {args.output}")
    print(f"  API Post: {args.post_to_api or 'disabled'}")
    print(f"{'='*60}\n")

    # Shared state across cameras of the same store
    # (allows cross-camera re-entry and dedup to work)
    shared_session_manager = SessionManager()
    shared_reid_engine = ReIDEngine()

    all_stats = {}

    for video_path in videos:
        camera_id = args.camera or guess_camera_id(str(video_path))

        pipeline = DetectionPipeline(
            video_path=str(video_path),
            store_id=args.store,
            camera_id=camera_id,
            model_path=args.model,
            entry_line_y=args.entry_line_y,
            layout_path=layout_path,
            min_confidence=args.min_confidence,
            events_output=args.output,
            api_url=args.post_to_api,
            session_manager=shared_session_manager,
            reid_engine=shared_reid_engine,
            headless=not args.show,
        )

        stats = pipeline.run()
        all_stats[camera_id] = stats

    # Final aggregate summary
    print(f"\n{'='*60}")
    print("AGGREGATE PIPELINE SUMMARY")
    print(f"{'='*60}")
    total_events = sum(s.get("total_events", 0) for s in all_stats.values())
    total_entries = sum(s.get("entries", 0) for s in all_stats.values())
    total_exits = sum(s.get("exits", 0) for s in all_stats.values())
    total_reentries = sum(s.get("reentries", 0) for s in all_stats.values())
    print(f"  Total events emitted : {total_events}")
    print(f"  Total entries        : {total_entries}")
    print(f"  Total exits          : {total_exits}")
    print(f"  Total reentries      : {total_reentries}")
    print(f"  Events file          : {args.output}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
