from collections import Counter
from datetime import datetime, timezone
import io
import json
import os
import sqlite3
import uuid
import zipfile
from xml.sax.saxutils import escape

import cv2
from flask import Flask, jsonify, render_template, request, send_file
from pymongo import MongoClient
from werkzeug.utils import secure_filename

from face_recognition import FaceRecognition

app = Flask(__name__)

# MongoDB setup
MONGO_URI = "mongodb+srv://charan:charan123@cluster0.tilwtgy.mongodb.net/"
mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client["attendance_db"]
attendance_collection = mongo_db["attendance_records"]

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


def sanitize_student_name(name):
    cleaned_name = (name or "").strip()
    if not cleaned_name or cleaned_name.startswith("."):
        return None
    return cleaned_name


def normalize_student_names(names):
    normalized_names = set()
    for name in names:
        cleaned_name = sanitize_student_name(name)
        if cleaned_name:
            normalized_names.add(cleaned_name)
    return sorted(normalized_names)


def get_registered_students():
    conn = sqlite3.connect("students_attendance.db")
    c = conn.cursor()
    c.execute("SELECT name FROM students ORDER BY name")
    rows = normalize_student_names(row[0] for row in c.fetchall())
    conn.close()

    if rows:
        return rows

    return normalize_student_names(face_recognition_instance.HumanNames)


def build_attendance_rows(present_students, absentees):
    status_by_name = {}

    for name in absentees:
        cleaned_name = sanitize_student_name(name)
        if cleaned_name:
            status_by_name[cleaned_name] = "Absent"

    for name in present_students:
        cleaned_name = sanitize_student_name(name)
        if cleaned_name:
            status_by_name[cleaned_name] = "Present"

    return [
        {"sno": index, "name": name, "status": status_by_name[name]}
        for index, name in enumerate(sorted(status_by_name), start=1)
    ]


def build_attendance_workbook(attendance_rows):
    def inline_string_cell(cell_ref, value):
        return (
            f'<c r="{cell_ref}" t="inlineStr">'
            f"<is><t>{escape(str(value))}</t></is>"
            "</c>"
        )

    def number_cell(cell_ref, value):
        return f'<c r="{cell_ref}"><v>{value}</v></c>'

    sheet_rows = [
        "<row r=\"1\">"
        f"{inline_string_cell('A1', 'S.No')}"
        f"{inline_string_cell('B1', 'Name')}"
        f"{inline_string_cell('C1', 'Present/Absent')}"
        "</row>"
    ]

    for row_index, row in enumerate(attendance_rows, start=2):
        try:
            serial_number = int(row.get("sno", row_index - 1))
        except (TypeError, ValueError):
            serial_number = row_index - 1

        student_name = sanitize_student_name(row.get("name")) or ""
        status = "Present" if str(row.get("status", "")).strip().lower() == "present" else "Absent"

        sheet_rows.append(
            f'<row r="{row_index}">'
            f"{number_cell(f'A{row_index}', serial_number)}"
            f"{inline_string_cell(f'B{row_index}', student_name)}"
            f"{inline_string_cell(f'C{row_index}', status)}"
            "</row>"
        )

    max_row = len(attendance_rows) + 1
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="Attendance" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )

    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:C{max_row}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        "<cols>"
        '<col min="1" max="1" width="10" customWidth="1"/>'
        '<col min="2" max="2" width="28" customWidth="1"/>'
        '<col min="3" max="3" width="18" customWidth="1"/>'
        "</cols>"
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    root_relationships_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/>'
        "</Relationships>"
    )

    workbook_relationships_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    app_properties_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>Microsoft Excel</Application>"
        "</Properties>"
    )

    core_properties_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:creator>Video-based-attendance</dc:creator>"
        "<cp:lastModifiedBy>Video-based-attendance</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )

    workbook_stream = io.BytesIO()
    with zipfile.ZipFile(workbook_stream, "w", compression=zipfile.ZIP_DEFLATED) as workbook_archive:
        workbook_archive.writestr("[Content_Types].xml", content_types_xml)
        workbook_archive.writestr("_rels/.rels", root_relationships_xml)
        workbook_archive.writestr("docProps/app.xml", app_properties_xml)
        workbook_archive.writestr("docProps/core.xml", core_properties_xml)
        workbook_archive.writestr("xl/workbook.xml", workbook_xml)
        workbook_archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships_xml)
        workbook_archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)

    workbook_stream.seek(0)
    return workbook_stream


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
        present_students = set()
        for name, count in detection_counts.items():
            if count < MIN_PRESENT_DETECTIONS:
                continue

            cleaned_name = sanitize_student_name(name)
            if cleaned_name:
                present_students.add(cleaned_name)

        registered_students = set(get_registered_students())
        absentees = registered_students - present_students
        attendance_rows = build_attendance_rows(present_students, absentees)

        face_recognition_instance.update_attendance(present_students, subject, teacher)
        face_recognition_instance.mark_absentees(absentees, subject, teacher)

        # Save attendance to MongoDB
        current_date = datetime.now().strftime("%Y-%m-%d")
        mongo_records = []
        for name in present_students:
            mongo_records.append({
                "student_name": name,
                "subject": subject,
                "teacher": teacher,
                "date": current_date,
                "status": "Present",
            })
        for name in absentees:
            mongo_records.append({
                "student_name": name,
                "subject": subject,
                "teacher": teacher,
                "date": current_date,
                "status": "Absent",
            })
        if mongo_records:
            attendance_collection.insert_many(mongo_records)

        return render_template(
            "results.html",
            detected_names=sorted(present_students),
            absentees=sorted(absentees),
            processed_frames=processed_frames,
            total_frames=total_frames,
            attendance_rows=attendance_rows,
            subject=subject,
        )
    except Exception as e:
        print(f"Error: {e}")
        return "An error occurred while processing the recording", 500
    finally:
        if "video_path" in locals() and os.path.exists(video_path):
            os.remove(video_path)


@app.route("/download_attendance", methods=["POST"])
def download_attendance():
    attendance_rows_payload = request.form.get("attendance_rows", "[]")
    subject = request.form.get("subject", "").strip()

    try:
        parsed_rows = json.loads(attendance_rows_payload)
    except json.JSONDecodeError:
        return "Invalid attendance data", 400

    if not isinstance(parsed_rows, list):
        return "Invalid attendance data", 400

    workbook_stream = build_attendance_workbook(parsed_rows)
    safe_subject = secure_filename(subject) or "attendance"

    return send_file(
        workbook_stream,
        as_attachment=True,
        download_name=f"{safe_subject}_attendance.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/mark_present", methods=["POST"])
def mark_present():
    data = request.get_json()
    names = data.get("names", [])
    subject = data.get("subject", "").strip()

    if not names:
        return jsonify({"success": False, "error": "No names provided"}), 400

    current_date = datetime.now().strftime("%Y-%m-%d")

    # Update SQLite
    conn = sqlite3.connect("students_attendance.db")
    c = conn.cursor()
    for name in names:
        cleaned = sanitize_student_name(name)
        if not cleaned:
            continue
        c.execute("SELECT id FROM students WHERE name = ?", (cleaned,))
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE attendance SET status = 'Present' WHERE student_id = ? AND date = ? AND subject = ?",
                (row[0], current_date, subject),
            )
    conn.commit()
    conn.close()

    # Update MongoDB
    for name in names:
        cleaned = sanitize_student_name(name)
        if not cleaned:
            continue
        attendance_collection.update_one(
            {"student_name": cleaned, "date": current_date, "subject": subject},
            {"$set": {"status": "Present"}},
        )

    return jsonify({"success": True})


@app.route("/attendance_percentage")
def attendance_percentage():
    pipeline = [
        {
            "$group": {
                "_id": "$student_name",
                "total": {"$sum": 1},
                "present": {
                    "$sum": {"$cond": [{"$eq": ["$status", "Present"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]
    results = list(attendance_collection.aggregate(pipeline))
    students = []
    for r in results:
        percentage = round((r["present"] / r["total"]) * 100, 2) if r["total"] > 0 else 0
        students.append({
            "name": r["_id"],
            "total_classes": r["total"],
            "present": r["present"],
            "absent": r["total"] - r["present"],
            "percentage": percentage,
        })
    return jsonify(students)


if __name__ == "__main__":
    app.run(debug=True)
