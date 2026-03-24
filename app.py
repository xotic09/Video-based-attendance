from collections import Counter
import os
import sqlite3
import uuid

import cv2
from flask import Flask, render_template, request
from werkzeug.utils import secure_filename

from face_recognition import FaceRecognition

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"webm", "mp4", "mov", "avi", "mkv"}
FRAME_SAMPLE_INTERVAL = 15
MIN_PRESENT_DETECTIONS = 2

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def init_db():
    conn = sqlite3.connect("students_attendance.db")
    c = conn.cursor()
    c.execute(
        """
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY,
        name TEXT,
        sno TEXT,
        roll_no TEXT,
        class TEXT
    )
    """
    )
    c.execute(
        """
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY,
        student_id INTEGER,
        date TEXT NOT NULL,
        subject TEXT,
        teacher TEXT,
        status TEXT,
        FOREIGN KEY (student_id) REFERENCES students (id)
    )
    """
    )
    conn.commit()
    conn.close()


init_db()
face_recognition_instance = FaceRecognition()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_registered_students():
    conn = sqlite3.connect("students_attendance.db")
    c = conn.cursor()
    c.execute("SELECT name FROM students ORDER BY name")
    rows = [row[0] for row in c.fetchall()]
    conn.close()

    if rows:
        return rows

    return list(face_recognition_instance.HumanNames)


def process_video(video_path, frame_interval=FRAME_SAMPLE_INTERVAL):
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError("Unable to open uploaded video")

    detection_counts = Counter()
    processed_frames = 0
    frame_index = 0

    try:
        while True:
            success, frame = capture.read()
            if not success:
                break

            if frame_index % frame_interval == 0:
                _, detected_names = face_recognition_instance.recognize_faces(frame)
                detection_counts.update(detected_names)
                processed_frames += 1

            frame_index += 1
    finally:
        capture.release()

    return detection_counts, processed_frames, frame_index


@app.route("/")
def index():
    return render_template(
        "index.html",
        frame_interval=FRAME_SAMPLE_INTERVAL,
        min_present_detections=MIN_PRESENT_DETECTIONS,
    )


@app.route("/upload_recording", methods=["POST"])
def upload_recording():
    try:
        subject = request.form["subject"].strip()
        teacher = request.form["teacher"].strip()
        video_file = request.files.get("video")

        if not video_file or not video_file.filename:
            return "No recording was uploaded", 400

        if not allowed_file(video_file.filename):
            return "Unsupported video format", 400

        filename = secure_filename(video_file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        video_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        video_file.save(video_path)

        detection_counts, processed_frames, total_frames = process_video(video_path)
        present_students = {
            name for name, count in detection_counts.items() if count >= MIN_PRESENT_DETECTIONS
        }
        registered_students = set(get_registered_students())
        absentees = registered_students - present_students

        face_recognition_instance.update_attendance(present_students, subject, teacher)
        face_recognition_instance.mark_absentees(absentees, subject, teacher)

        return render_template(
            "results.html",
            detected_names=sorted(present_students),
            absentees=sorted(absentees),
            detection_counts=dict(sorted(detection_counts.items())),
            processed_frames=processed_frames,
            total_frames=total_frames,
            min_present_detections=MIN_PRESENT_DETECTIONS,
        )
    except Exception as e:
        print(f"Error: {e}")
        return "An error occurred while processing the recording", 500
    finally:
        if "video_path" in locals() and os.path.exists(video_path):
            os.remove(video_path)


if __name__ == "__main__":
    app.run(debug=True)
