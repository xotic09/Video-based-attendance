from collections import Counter
from datetime import datetime, timezone, timedelta
from functools import wraps
import io
import json
import os
import random
import sqlite3
import string
import uuid
import zipfile
from xml.sax.saxutils import escape

import certifi
import cv2
import jwt
from bson import ObjectId
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from pymongo import MongoClient
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from face_recognition import FaceRecognition

app = Flask(__name__)
app.config["SECRET_KEY"] = "attendance-jwt-secret-key-2024"

# MongoDB setup
MONGO_URI = "mongodb+srv://charan:charan123@cluster0.tilwtgy.mongodb.net/"
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
mongo_db = mongo_client["attendance_db"]
attendance_collection = mongo_db["attendance_records"]
users_collection = mongo_db["users"]
classes_collection = mongo_db["classes"]

UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"webm", "mp4", "mov", "avi", "mkv"}
FRAME_SAMPLE_INTERVAL = 15
MIN_PRESENT_DETECTIONS = 2

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ─── Database Init ───────────────────────────────────────────────────


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


def seed_data():
    """Create default admin user and dummy classes if they don't exist."""
    if not users_collection.find_one({"username": "admin"}):
        users_collection.insert_one(
            {
                "username": "admin",
                "password": generate_password_hash("Admin@123"),
                "role": "admin",
                "full_name": "Administrator",
            }
        )

    if classes_collection.count_documents({}) == 0:
        classes_collection.insert_many(
            [
                {"code": "CS101", "name": "Data Structures", "assigned_teacher": None, "enrolled_students": []},
                {"code": "CS102", "name": "Algorithms", "assigned_teacher": None, "enrolled_students": []},
                {"code": "CS103", "name": "Database Systems", "assigned_teacher": None, "enrolled_students": []},
                {"code": "CS104", "name": "Operating Systems", "assigned_teacher": None, "enrolled_students": []},
                {"code": "CS105", "name": "Computer Networks", "assigned_teacher": None, "enrolled_students": []},
            ]
        )

    # Migrate existing classes to include enrolled_students field
    classes_collection.update_many(
        {"enrolled_students": {"$exists": False}},
        {"$set": {"enrolled_students": []}},
    )


init_db()
seed_data()
face_recognition_instance = FaceRecognition()


# ─── JWT Auth Helpers ────────────────────────────────────────────────


def create_token(username, role):
    payload = {
        "username": username,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=24),
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")


def get_current_user():
    token = request.cookies.get("token")
    if not token:
        return None
    try:
        return jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        request.user = user
        return f(*args, **kwargs)

    return decorated


def role_required(role):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] != role:
                return "Access denied", 403
            request.user = user
            return f(*args, **kwargs)

        return decorated

    return decorator


# ─── Utility Functions ───────────────────────────────────────────────


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def sanitize_student_name(name):
    cleaned_name = (name or "").strip()
    if not cleaned_name or cleaned_name.startswith("."):
        return None
    return cleaned_name


def normalize_student_lookup_value(value):
    cleaned_value = sanitize_student_name(value)
    return cleaned_value.casefold() if cleaned_value else None


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


# ─── Auth Routes ─────────────────────────────────────────────────────


@app.route("/")
def home():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return redirect(url_for(f"{user['role']}_dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        user = get_current_user()
        if user:
            return redirect(url_for("home"))
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    user = users_collection.find_one({"username": username})
    if not user or not check_password_hash(user["password"], password):
        return render_template("login.html", error="Invalid username or password")

    token = create_token(username, user["role"])
    response = make_response(redirect(url_for("home")))
    response.set_cookie("token", token, httponly=True, max_age=86400)
    return response


def generate_password(length=8):
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


@app.route("/logout")
def logout():
    response = make_response(redirect(url_for("login")))
    response.delete_cookie("token")
    return response


# ─── Admin Routes ────────────────────────────────────────────────────


@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    classes = list(classes_collection.find())
    teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
    students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
    return render_template(
        "admin_dashboard.html",
        classes=classes,
        teachers=teachers,
        students=students,
        user=request.user,
    )


@app.route("/admin/assign", methods=["POST"])
@role_required("admin")
def assign_teacher():
    class_id = request.form.get("class_id")
    teacher_username = request.form.get("teacher_username")

    classes_collection.update_one(
        {"_id": ObjectId(class_id)},
        {"$set": {"assigned_teacher": teacher_username}},
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/unassign", methods=["POST"])
@role_required("admin")
def unassign_teacher():
    class_id = request.form.get("class_id")

    classes_collection.update_one(
        {"_id": ObjectId(class_id)},
        {"$set": {"assigned_teacher": None}},
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/add-class", methods=["POST"])
@role_required("admin")
def add_class():
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()

    if not code or not name:
        return redirect(url_for("admin_dashboard"))

    if classes_collection.find_one({"code": code}):
        return redirect(url_for("admin_dashboard"))

    classes_collection.insert_one(
        {"code": code, "name": name, "assigned_teacher": None, "enrolled_students": []}
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/class/<class_id>")
@role_required("admin")
def admin_class_detail(class_id):
    cls = classes_collection.find_one({"_id": ObjectId(class_id)})
    if not cls:
        return redirect(url_for("admin_dashboard"))

    teacher = None
    if cls.get("assigned_teacher"):
        teacher = users_collection.find_one(
            {"username": cls["assigned_teacher"]}, {"password": 0, "raw_password": 0}
        )

    enrolled = []
    for username in cls.get("enrolled_students", []):
        student = users_collection.find_one(
            {"username": username}, {"password": 0, "raw_password": 0}
        )
        if student:
            enrolled.append(student)
        else:
            enrolled.append({"username": username, "full_name": username})

    return render_template(
        "admin_class_detail.html",
        cls=cls,
        teacher=teacher,
        enrolled=enrolled,
        user=request.user,
    )


@app.route("/admin/delete-class", methods=["POST"])
@role_required("admin")
def delete_class():
    class_id = request.form.get("class_id")
    if class_id:
        classes_collection.delete_one({"_id": ObjectId(class_id)})
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/enroll")
@role_required("admin")
def admin_enroll():
    classes = list(classes_collection.find())
    students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
    selected_class = request.args.get("class_id")
    return render_template(
        "admin_enroll.html",
        classes=classes,
        students=students,
        selected_class=selected_class,
        user=request.user,
    )


@app.route("/admin/enroll-students", methods=["POST"])
@role_required("admin")
def enroll_students():
    class_id = request.form.get("class_id")
    student_usernames = request.form.getlist("student_usernames")

    if class_id and student_usernames:
        classes_collection.update_one(
            {"_id": ObjectId(class_id)},
            {"$addToSet": {"enrolled_students": {"$each": student_usernames}}},
        )
    return redirect(url_for("admin_enroll", class_id=class_id))


@app.route("/admin/unenroll-student", methods=["POST"])
@role_required("admin")
def unenroll_student():
    class_id = request.form.get("class_id")
    student_username = request.form.get("student_username")

    if class_id and student_username:
        classes_collection.update_one(
            {"_id": ObjectId(class_id)},
            {"$pull": {"enrolled_students": student_username}},
        )
    return redirect(url_for("admin_enroll", class_id=class_id))


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
    students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
    return render_template(
        "admin_users.html",
        teachers=teachers,
        students=students,
        user=request.user,
    )


@app.route("/admin/create-user", methods=["POST"])
@role_required("admin")
def create_user():
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    role = request.form.get("role", "student")

    if not username or not full_name:
        teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
        students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
        return render_template(
            "admin_users.html",
            teachers=teachers,
            students=students,
            user=request.user,
            error="Username and full name are required",
        )

    if role not in ("teacher", "student"):
        teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
        students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
        return render_template(
            "admin_users.html",
            teachers=teachers,
            students=students,
            user=request.user,
            error="Invalid role",
        )

    if users_collection.find_one({"username": username}):
        teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
        students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
        return render_template(
            "admin_users.html",
            teachers=teachers,
            students=students,
            user=request.user,
            error=f"Username '{username}' already exists",
        )

    raw_password = generate_password()
    users_collection.insert_one(
        {
            "username": username,
            "password": generate_password_hash(raw_password),
            "raw_password": raw_password,
            "role": role,
            "full_name": full_name,
        }
    )

    teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
    students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
    return render_template(
        "admin_users.html",
        teachers=teachers,
        students=students,
        user=request.user,
        show_username=username,
    )


@app.route("/admin/delete-user", methods=["POST"])
@role_required("admin")
def delete_user():
    username = request.form.get("username", "").strip()
    if username and username != "admin":
        # Unassign from any classes if teacher
        classes_collection.update_many(
            {"assigned_teacher": username},
            {"$set": {"assigned_teacher": None}},
        )
        # Remove from enrolled_students if student
        classes_collection.update_many(
            {"enrolled_students": username},
            {"$pull": {"enrolled_students": username}},
        )
        users_collection.delete_one({"username": username})
    return redirect(url_for("admin_users"))


@app.route("/admin/reset-password", methods=["POST"])
@role_required("admin")
def reset_password():
    username = request.form.get("username", "").strip()
    if not username or username == "admin":
        return redirect(url_for("admin_users"))

    user_doc = users_collection.find_one({"username": username})
    if not user_doc:
        return redirect(url_for("admin_users"))

    raw_password = generate_password()
    users_collection.update_one(
        {"username": username},
        {"$set": {"password": generate_password_hash(raw_password), "raw_password": raw_password}},
    )

    teachers = list(users_collection.find({"role": "teacher"}, {"password": 0, "_id": 0}))
    students = list(users_collection.find({"role": "student"}, {"password": 0, "_id": 0}))
    return render_template(
        "admin_users.html",
        teachers=teachers,
        students=students,
        user=request.user,
        show_username=username,
    )


# ─── Teacher Routes ──────────────────────────────────────────────────


@app.route("/teacher")
@role_required("teacher")
def teacher_dashboard():
    assigned_classes = list(
        classes_collection.find({"assigned_teacher": request.user["username"]})
    )
    return render_template(
        "teacher_dashboard.html",
        classes=assigned_classes,
        user=request.user,
    )


@app.route("/teacher/attendance/<class_id>")
@role_required("teacher")
def take_attendance(class_id):
    cls = classes_collection.find_one(
        {"_id": ObjectId(class_id), "assigned_teacher": request.user["username"]}
    )
    if not cls:
        return "Class not found or not assigned to you", 403

    teacher_doc = users_collection.find_one({"username": request.user["username"]})
    teacher_name = teacher_doc["full_name"] if teacher_doc else request.user["username"]

    return render_template(
        "index.html",
        frame_interval=FRAME_SAMPLE_INTERVAL,
        min_present_detections=MIN_PRESENT_DETECTIONS,
        class_info=cls,
        teacher_name=teacher_name,
        user=request.user,
    )


# ─── Student Routes ─────────────────────────────────────────────────


@app.route("/student")
@role_required("student")
def student_dashboard():
    username = request.user["username"]
    student_doc = users_collection.find_one(
        {"username": username},
        {"_id": 0, "full_name": 1},
    ) or {}

    student_identifiers = []
    for candidate in (username, student_doc.get("full_name")):
        cleaned_candidate = sanitize_student_name(candidate)
        if cleaned_candidate and cleaned_candidate not in student_identifiers:
            student_identifiers.append(cleaned_candidate)

    normalized_identifiers = set()
    for identifier in student_identifiers:
        normalized_identifier = normalize_student_lookup_value(identifier)
        if normalized_identifier:
            normalized_identifiers.add(normalized_identifier)

    # Get all classes the student is enrolled in.
    # Older data may store either username or full name, so match both safely.
    enrolled_classes = []
    for cls in classes_collection.find():
        enrolled_students = cls.get("enrolled_students", [])
        if not isinstance(enrolled_students, list):
            enrolled_students = [enrolled_students] if enrolled_students else []

        normalized_enrolled_students = set()
        for student_name in enrolled_students:
            normalized_student_name = normalize_student_lookup_value(student_name)
            if normalized_student_name:
                normalized_enrolled_students.add(normalized_student_name)
        if normalized_enrolled_students & normalized_identifiers:
            enrolled_classes.append(cls)

    # Build subject keys matching the format used in attendance records: "CODE - NAME"
    class_subject_map = {}
    for cls in enrolled_classes:
        subject_key = f"{cls['code']} - {cls['name']}"
        class_subject_map[subject_key] = cls

    if not enrolled_classes:
        return render_template(
            "student_dashboard.html", subjects=[], user=request.user
        )

    # Fetch attendance for this student across enrolled subjects
    subject_keys = list(class_subject_map.keys())
    pipeline = [
        {"$match": {"subject": {"$in": subject_keys}}},
        {
            "$addFields": {
                "student_name_normalized": {
                    "$toLower": {"$trim": {"input": {"$ifNull": ["$student_name", ""]}}}
                }
            }
        },
        {"$match": {"student_name_normalized": {"$in": list(normalized_identifiers)}}},
        {
            "$group": {
                "_id": "$subject",
                "total": {"$sum": 1},
                "present": {
                    "$sum": {"$cond": [{"$eq": ["$status", "Present"]}, 1, 0]}
                },
            }
        },
    ]
    results = {r["_id"]: r for r in attendance_collection.aggregate(pipeline)}

    # Build subjects list — show ALL enrolled classes, even with 0 attendance
    subjects = []
    for subject_key in sorted(class_subject_map.keys()):
        cls = class_subject_map[subject_key]
        r = results.get(subject_key)
        if r:
            total = r["total"]
            present = r["present"]
            percentage = round((present / total) * 100, 2) if total > 0 else 0
        else:
            total = 0
            present = 0
            percentage = 0

        percentage_label = (
            f"{int(percentage)}%"
            if float(percentage).is_integer()
            else f"{percentage:.2f}%"
        )

        subjects.append(
            {
                "code": cls["code"],
                "subject": cls["name"],
                "total_classes": total,
                "present": present,
                "absent": total - present,
                "percentage": percentage,
                "percentage_label": percentage_label,
            }
        )

    return render_template(
        "student_dashboard.html", subjects=subjects, user=request.user
    )


# ─── Core Processing Routes ─────────────────────────────────────────


@app.route("/upload_recording", methods=["POST"])
@login_required
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

        # Use enrolled students if class_id provided, otherwise fall back to all registered
        class_id = request.form.get("class_id")
        if class_id:
            cls = classes_collection.find_one({"_id": ObjectId(class_id)})
            enrolled = set(cls.get("enrolled_students", [])) if cls else set()
            registered_students = enrolled
            # Only count present students who are enrolled
            present_students = present_students & enrolled
        else:
            registered_students = set(get_registered_students())

        absentees = registered_students - present_students
        attendance_rows = build_attendance_rows(present_students, absentees)

        face_recognition_instance.update_attendance(present_students, subject, teacher)
        face_recognition_instance.mark_absentees(absentees, subject, teacher)

        # Save attendance to MongoDB
        current_date = datetime.now().strftime("%Y-%m-%d")
        mongo_records = []
        for name in present_students:
            mongo_records.append(
                {
                    "student_name": name,
                    "subject": subject,
                    "teacher": teacher,
                    "date": current_date,
                    "status": "Present",
                }
            )
        for name in absentees:
            mongo_records.append(
                {
                    "student_name": name,
                    "subject": subject,
                    "teacher": teacher,
                    "date": current_date,
                    "status": "Absent",
                }
            )
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
            class_id=class_id or "",
            user=getattr(request, "user", None),
        )
    except Exception as e:
        print(f"Error: {e}")
        return "An error occurred while processing the recording", 500
    finally:
        if "video_path" in locals() and os.path.exists(video_path):
            os.remove(video_path)


@app.route("/download_attendance", methods=["POST"])
@login_required
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
@login_required
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
@login_required
def attendance_percentage():
    subject_filter = request.args.get("subject", "").strip()
    class_id_filter = request.args.get("class_id", "").strip()

    match_stage = {}
    if subject_filter:
        match_stage["subject"] = subject_filter

    # Resolve enrolled students — try class_id first, then match subject to a class
    enrolled = None
    if class_id_filter:
        try:
            cls = classes_collection.find_one({"_id": ObjectId(class_id_filter)})
            if cls:
                enrolled = cls.get("enrolled_students", [])
        except Exception:
            pass

    if enrolled is None and subject_filter:
        # Subject format is "CS101 - Data Structures", try to find matching class
        parts = subject_filter.split(" - ", 1)
        if len(parts) == 2:
            cls = classes_collection.find_one({"code": parts[0].strip(), "name": parts[1].strip()})
            if cls:
                enrolled = cls.get("enrolled_students", [])

    if enrolled is not None and enrolled:
        match_stage["student_name"] = {"$in": enrolled}
    elif enrolled is not None:
        # Class found but no students enrolled — return empty
        return jsonify([])

    pipeline = []
    if match_stage:
        pipeline.append({"$match": match_stage})

    pipeline.extend([
        {
            "$group": {
                "_id": {"student": "$student_name", "subject": "$subject"},
                "total": {"$sum": 1},
                "present": {
                    "$sum": {"$cond": [{"$eq": ["$status", "Present"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id.student": 1, "_id.subject": 1}},
    ])
    results = list(attendance_collection.aggregate(pipeline))
    students = []
    for r in results:
        percentage = (
            round((r["present"] / r["total"]) * 100, 2) if r["total"] > 0 else 0
        )
        students.append(
            {
                "name": r["_id"]["student"],
                "subject": r["_id"]["subject"],
                "total_classes": r["total"],
                "present": r["present"],
                "absent": r["total"] - r["present"],
                "percentage": percentage,
            }
        )
    return jsonify(students)


if __name__ == "__main__":
    app.run(debug=True)
