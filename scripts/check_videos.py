"""Check all video files in data/ directory."""
import cv2, os

data_dir = "data"
for f in sorted(os.listdir(data_dir)):
    if f.endswith(".mp4"):
        path = os.path.join(data_dir, f)
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frames / fps if fps > 0 else 0
        print(f"{f}")
        print(f"  FPS={fps:.1f}  Resolution={w}x{h}  Frames={frames}  Duration={duration:.1f}s ({duration/60:.1f}min)")
        cap.release()
