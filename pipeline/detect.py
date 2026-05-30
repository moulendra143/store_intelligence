from ultralytics import YOLO
import cv2

from emit import create_event, save_event
from session_manager import SessionManager

# ---------------------------------
# CONFIG
# ---------------------------------
VIDEO_PATH = "data/CAM 2.mp4"

STORE_ID = "STORE_BLR_002"
CAMERA_ID = "CAM_ENTRY_02"

ENTRY_LINE_Y = 550

MIN_CONFIDENCE = 0.40

# ---------------------------------
# LOAD MODEL
# ---------------------------------
model = YOLO("yolov8n.pt")

# ---------------------------------
# SESSION MANAGER
# ---------------------------------
session_manager = SessionManager()

# ---------------------------------
# VIDEO
# ---------------------------------
cap = cv2.VideoCapture(VIDEO_PATH)

if not cap.isOpened():
    print(f"Error: Could not open video: {VIDEO_PATH}")
    exit()

print("Starting Store Intelligence Detection Pipeline...")

# ---------------------------------
# TRACKING STATE
# ---------------------------------
previous_positions = {}

inside_store = set()

known_visitors = set()

# ---------------------------------
# MAIN LOOP
# ---------------------------------
while True:

    success, frame = cap.read()

    if not success:
        print("Video processing completed.")
        break

    results = model.track(
        frame,
        persist=True,
        tracker="bytetrack.yaml",
        classes=[0],
        verbose=False
    )

    annotated_frame = results[0].plot()

    # Draw Entry/Exit Line
    cv2.line(
        annotated_frame,
        (0, ENTRY_LINE_Y),
        (annotated_frame.shape[1], ENTRY_LINE_Y),
        (0, 255, 0),
        2
    )

    cv2.putText(
        annotated_frame,
        "ENTRY / EXIT LINE",
        (20, ENTRY_LINE_Y - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 255, 0),
        2
    )

    if results[0].boxes is not None:

        for box in results[0].boxes:

            if box.id is None:
                continue

            track_id = int(box.id.item())

            confidence = float(box.conf.item())

            if confidence < MIN_CONFIDENCE:
                continue

            visitor_id = f"VIS_{track_id}"

            # Update session heartbeat
            if session_manager.has_session(visitor_id):
                session_manager.update_seen(visitor_id)

            # Bounding Box
            x1, y1, x2, y2 = box.xyxy[0]

            x1 = int(x1)
            y1 = int(y1)
            x2 = int(x2)
            y2 = int(y2)

            center_x = int((x1 + x2) / 2)
            center_y = int((y1 + y2) / 2)

            # Draw center point
            cv2.circle(
                annotated_frame,
                (center_x, center_y),
                5,
                (0, 0, 255),
                -1
            )

            # New visitor detected
            if visitor_id not in known_visitors:

                known_visitors.add(visitor_id)

                print(
                    f"NEW VISITOR -> {visitor_id} "
                    f"(confidence={confidence:.2f})"
                )

            # ---------------------------------
            # ENTRY / EXIT LOGIC
            # ---------------------------------
            if track_id in previous_positions:

                previous_y = previous_positions[track_id]

                # -----------------------------
                # ENTRY / REENTRY
                # -----------------------------
                if (
                    previous_y < ENTRY_LINE_Y
                    and center_y >= ENTRY_LINE_Y
                    and visitor_id not in inside_store
                ):

                    inside_store.add(visitor_id)

                    # First Entry
                    if not session_manager.has_session(visitor_id):

                        session_manager.create_session(visitor_id)

                        event = create_event(
                            store_id=STORE_ID,
                            camera_id=CAMERA_ID,
                            visitor_id=visitor_id,
                            event_type="ENTRY",
                            confidence=confidence
                        )

                        save_event(event)

                        print(f"\nENTRY -> {visitor_id}")
                        print(event)
                        print("-" * 60)

                    # Re-entry
                    elif session_manager.is_reentry(visitor_id):

                        session_manager.mark_reentry(visitor_id)

                        event = create_event(
                            store_id=STORE_ID,
                            camera_id=CAMERA_ID,
                            visitor_id=visitor_id,
                            event_type="REENTRY",
                            confidence=confidence
                        )

                        save_event(event)

                        print(f"\nREENTRY -> {visitor_id}")
                        print(event)
                        print("-" * 60)

                # -----------------------------
                # EXIT
                # -----------------------------
                elif (
                    previous_y > ENTRY_LINE_Y
                    and center_y <= ENTRY_LINE_Y
                    and visitor_id in inside_store
                ):

                    inside_store.remove(visitor_id)

                    session_manager.exit_session(visitor_id)

                    event = create_event(
                        store_id=STORE_ID,
                        camera_id=CAMERA_ID,
                        visitor_id=visitor_id,
                        event_type="EXIT",
                        confidence=confidence
                    )

                    save_event(event)

                    print(f"\nEXIT -> {visitor_id}")
                    print(event)
                    print("-" * 60)

            # Save current position
            previous_positions[track_id] = center_y

    cv2.imshow(
        "Store Intelligence Detection",
        annotated_frame
    )

    key = cv2.waitKey(1)

    if key & 0xFF == ord("q"):
        break

# ---------------------------------
# CLEANUP
# ---------------------------------
cap.release()
cv2.destroyAllWindows()

print("\nPipeline Finished")
print(f"Unique Visitors Seen: {len(known_visitors)}")
print(f"Visitors Currently Inside: {len(inside_store)}")

print("\nSESSION SUMMARY")

for visitor_id, session in session_manager.sessions.items():

    print(
        visitor_id,
        "inside =", session["inside"],
        "entry =", session["entry_time"],
        "exit =", session["exit_time"],
        "reentries =", session["reentry_count"]
    )