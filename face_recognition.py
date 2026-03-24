from __future__ import absolute_import, division, print_function
import cv2
import numpy as np
import facenet
import detect_face
import os
import time
import pickle
import sqlite3 
from PIL import Image
import tensorflow.compat.v1 as tf
from flask import Flask
from datetime import datetime
modeldir = './model/20180402-114759.pb'
classifier_filename = './class/classifier.pkl'
npy = './npy'
train_img = "./train_img"
captured_img_folder = './captured_images'

if not os.path.exists(captured_img_folder):
    os.makedirs(captured_img_folder)

class FaceRecognition:
    def __init__(self):
        self.sess = None
        self.pnet = None
        self.rnet = None
        self.onet = None
        self.model = None
        self.images_placeholder = None
        self.embeddings = None
        self.phase_train_placeholder = None
        self.embedding_size = None
        self.HumanNames = None

        self._load_model()
        self._load_classifier()

    def _load_model(self):
        with tf.Graph().as_default():
            gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.6)
            self.sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options, log_device_placement=False))
            with self.sess.as_default():
                self.pnet, self.rnet, self.onet = detect_face.create_mtcnn(self.sess, npy)
                print('Loading Model')
                facenet.load_model(modeldir)
                self.images_placeholder = tf.get_default_graph().get_tensor_by_name("input:0")
                self.embeddings = tf.get_default_graph().get_tensor_by_name("embeddings:0")
                self.phase_train_placeholder = tf.get_default_graph().get_tensor_by_name("phase_train:0")
                self.embedding_size = self.embeddings.get_shape()[1]
                self.HumanNames = os.listdir(train_img)
                self.HumanNames.sort()

    def _load_classifier(self):
        classifier_filename_exp = os.path.expanduser(classifier_filename)
        with open(classifier_filename_exp, 'rb') as infile:
            self.model, _ = pickle.load(infile, encoding='latin1')

    def recognize_faces(self, frame):
        bounding_boxes, _ = detect_face.detect_face(frame, 30, self.pnet, self.rnet, self.onet, [0.6, 0.7, 0.7], 0.709)
        faceNum = bounding_boxes.shape[0]
        detected_names = set()

        if faceNum > 0:
            det = bounding_boxes[:, 0:4]
            img_size = np.asarray(frame.shape)[0:2]
            cropped = []
            scaled = []
            scaled_reshape = []

            for i in range(faceNum):
                emb_array = np.zeros((1, self.embedding_size))
                xmin = int(det[i][0])
                ymin = int(det[i][1])
                xmax = int(det[i][2])
                ymax = int(det[i][3])

                try:
                    if xmin <= 0 or ymin <= 0 or xmax >= len(frame[0]) or ymax >= len(frame):
                        print('Face is very close!')
                        continue

                    cropped.append(frame[ymin:ymax, xmin:xmax, :])
                    cropped[i] = facenet.flip(cropped[i], False)
                    scaled.append(np.array(Image.fromarray(cropped[i]).resize((182, 182))))
                    scaled[i] = cv2.resize(scaled[i], (160, 160), interpolation=cv2.INTER_CUBIC)
                    scaled[i] = facenet.prewhiten(scaled[i])
                    scaled_reshape.append(scaled[i].reshape(-1, 160, 160, 3))
                    feed_dict = {self.images_placeholder: scaled_reshape[i], self.phase_train_placeholder: False}
                    emb_array[0, :] = self.sess.run(self.embeddings, feed_dict=feed_dict)
                    predictions = self.model.predict_proba(emb_array)
                    best_class_indices = np.argmax(predictions, axis=1)
                    best_class_probabilities = predictions[np.arange(len(best_class_indices)), best_class_indices]

                    if best_class_probabilities > 0.70:
                        result_name = self.HumanNames[best_class_indices[0]]
                        detected_names.add(result_name)
                        cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 255, 0), 2)
                        cv2.rectangle(frame, (xmin, ymin-20), (xmax, ymin-2), (0, 255, 255), -1)
                        cv2.putText(frame, result_name, (xmin, ymin-5), cv2.FONT_HERSHEY_COMPLEX_SMALL,
                                    1, (0, 0, 0), thickness=1, lineType=1)
                    else:
                        cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 255, 0), 2)
                        cv2.rectangle(frame, (xmin, ymin-20), (xmax, ymin-2), (0, 255, 255), -1)
                        cv2.putText(frame, "?", (xmin, ymin-5), cv2.FONT_HERSHEY_COMPLEX_SMALL,
                                    1, (0, 0, 0), thickness=1, lineType=1)

                except Exception as e:
                    print(f"Error: {e}")

        return frame, detected_names

    def capture_image(self, camera):
        success, frame = camera.read()
        if success:
            img_name = os.path.join(captured_img_folder, f"{time.strftime('%Y%m%d-%H%M%S')}.jpg")
            cv2.imwrite(img_name, frame)
            print(f"Captured and saved image: {img_name}")
            return img_name
        return None

    def update_attendance(self, detected_names, subject, teacher):
        try:
            conn = sqlite3.connect('students_attendance.db')
            c = conn.cursor()

        # Get the current date in 'YYYY-MM-DD' format
            current_date = datetime.now().strftime('%Y-%m-%d')

            for name in detected_names:
                student_id = self.get_student_id(name)
                if student_id and not self._attendance_exists(c, student_id, current_date, subject, teacher):
                # Insert attendance record with the current date
                    c.execute('''
                    INSERT INTO attendance (student_id, date, subject, teacher, status)
                    VALUES (?, ?, ?, ?, ?)
                    ''', (student_id, current_date, subject, teacher, 'Present'))
        
            conn.commit()
        except Exception as e:
            print(f"Error: {e}")
        finally:
            conn.close()  # Ensure the connection is always closed

    def mark_absentees(self, absentees, subject, teacher):
        try:
            conn = sqlite3.connect('students_attendance.db')
            c = conn.cursor()
            current_date = datetime.now().strftime('%Y-%m-%d')
        
            for absentee in absentees:
                student_id = self.get_student_id(absentee)
                if student_id and not self._attendance_exists(c, student_id, current_date, subject, teacher):
                    c.execute('''
                    INSERT INTO attendance (student_id, date, subject, teacher, status)
                    VALUES (?, ?, ?, ?, ?)
                    ''', (student_id, current_date, subject, teacher, 'Absent'))

            conn.commit()
        except Exception as e:
            print(f"Error: {e}")
        finally:
            conn.close()  # Ensure the connection is always closed

    def get_student_id(self, name):
        try:
            conn = sqlite3.connect('students_attendance.db')
            c = conn.cursor()
            c.execute('SELECT id FROM students WHERE name = ?', (name,))
            result = c.fetchone()
        except Exception as e:
            print(f"Error: {e}")
            result = None
        finally:
            conn.close()

        if result:
            return result[0]
        return None

    def _attendance_exists(self, cursor, student_id, current_date, subject, teacher):
        cursor.execute(
            '''
            SELECT 1
            FROM attendance
            WHERE student_id = ? AND date = ? AND subject = ? AND teacher = ?
            LIMIT 1
            ''',
            (student_id, current_date, subject, teacher)
        )
        return cursor.fetchone() is not None



# Ensure time module is imported
import time
