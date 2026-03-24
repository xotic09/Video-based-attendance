from flask import Flask, render_template, Response, request, redirect, url_for
import cv2
import sqlite3
from face_recognition import FaceRecognition

app = Flask(__name__)

# Initialize the database
def init_db():
    conn = sqlite3.connect('students_attendance.db')
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY,
        name TEXT,
        sno TEXT,
        roll_no TEXT,
        class TEXT
    )
    ''')
    c.execute('''
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY,
        student_id INTEGER,
        date TEXT NOT NULL,
        subject TEXT,
        teacher TEXT,
        status TEXT,
        FOREIGN KEY (student_id) REFERENCES students (id)
    )
    ''')
    conn.commit()
    conn.close()

# Initialize face recognition
face_recognition_instance = FaceRecognition()

# Initialize camera
camera = cv2.VideoCapture(0)

def gen_frames():
    while True:
        success, frame = camera.read()
        if not success:
            break
        else:
            frame, _ = face_recognition_instance.recognize_faces(frame)
            ret, buffer = cv2.imencode('.jpg', frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/capture', methods=['POST'])
def capture():
    try:
        success, frame = camera.read()
        if success:
            _, detected_names = face_recognition_instance.recognize_faces(frame)
            subject = request.form['subject']
            teacher = request.form['teacher']

            face_recognition_instance.update_attendance(detected_names, subject, teacher)
            absentees = set(face_recognition_instance.HumanNames) - detected_names
            face_recognition_instance.mark_absentees(absentees, subject, teacher)
            
            return render_template('results.html', detected_names=detected_names, absentees=absentees)
        else:
            return "Failed to capture image", 500
    except Exception as e:
        print(f"Error: {e}")
        return "An error occurred while processing the image", 500


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
