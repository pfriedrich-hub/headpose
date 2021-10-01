from pathlib import Path
import cv2
import numpy as np
import logging
from matplotlib import pyplot as plt
from PIL import Image
import torch
import torchvision.transforms.functional as TF
from headpose.network import ResNet
root = Path(__file__).parent
face_cascade = cv2.CascadeClassifier(str(root/"haarcascade_frontalface_default.xml"))


class PoseEstimator:
    def __init__(self, method):
        if method == "landmarks":
            self.model = ResNet()
            # TODO: check if the network is present, if not download it from the repo
            self.model.load_state_dict(torch.load(root/"model_weights.zip"))
        elif method == "aruco":
            pass
        else:
            raise ValueError("Possible methods are 'landmarks' or 'aruco'!")

    def _detect_landmarks(self, image):
        if image.ndim == 3:  # convert color to grayscale
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = image.shape
        faces = face_cascade.detectMultiScale(image, 1.1, 4)
        if len(faces) != 1:
            raise ValueError("There must be exactly one face in the image!")
        all_landmarks = []
        for (x, y, w, h) in faces:
            image = image[y:y + h, x:x + w]
            image = TF.resize(Image.fromarray(image), size=(224, 224))
            image = TF.to_tensor(image)
            image = TF.normalize(image, [0.5], [0.5])
        with torch.no_grad():
            landmarks = self.model(image.unsqueeze(0))
        landmarks = (landmarks.view(68, 2).detach().numpy()+0.5) * np.array([w, h]) + np.array([x, y])
        return landmarks

    def pose_from_image(self, image):
        size = image.shape
        focal_length = size[1]
        center = (size[1]/2, size[0]/2)
        camera_matrix = np.array([[focal_length, 0, center[0]],
                                 [0, focal_length, center[1]],
                                 [0, 0, 1]], dtype="double")

        faceboxes = self.extract_cnn_facebox(image)
        if len(faceboxes) > 1:
            logging.warning("There is more than one face in the image!")
            return None, None
        elif len(faceboxes) == 0:
            logging.warning("No face detected!")
            return None, None
        else:
            facebox = faceboxes[0]
            face_img = image[facebox[1]: facebox[3], facebox[0]: facebox[2]]
            face_img = cv2.resize(face_img, (128, 128))
            face_img = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
            marks = self.detect_marks([face_img])
            marks *= (facebox[2] - facebox[0])
            marks[:, 0] += facebox[0]
            marks[:, 1] += facebox[1]
            shape = marks.astype(np.uint)
            image_pts = np.float32([shape[17], shape[21], shape[22], shape[26],
                                    shape[36], shape[39], shape[42], shape[45],
                                    shape[31], shape[35], shape[48], shape[54],
                                    shape[57], shape[8]])
            dist_coeffs = np.zeros((4, 1))  # Assuming no lens distortion
            (success, rotation_vec, translation_vec) = \
                cv2.solvePnP(self.model_points, image_pts, camera_matrix,
                             dist_coeffs)

            rotation_mat, _ = cv2.Rodrigues(rotation_vec)
            pose_mat = cv2.hconcat((rotation_mat, translation_vec))
            _, _, _, _, _, _, angles = cv2.decomposeProjectionMatrix(pose_mat)
            angles[0, 0] = angles[0, 0] * -1

            return angles[1, 0], angles[0, 0], angles[2, 0]  # roll, pitch, yaw

    def get_faceboxes(self, image):
        """
        Get the bounding box of faces in image using dnn.
        """
        if image.ndim == 2:  # if greyscale, "fake" 3-channel image
            image = np.repeat(image[..., np.newaxis], 3, axis=2)
            mean = int(image[:, :, 0].mean())
        else:  # if image is RGB, subtract mean for each channel
            mean = (int(image[:, :, 0].mean()), int(image[:, :, 1].mean()),
                    int(image[:, :, 2].mean()))
        rows, cols, _ = image.shape
        confidences, faceboxes = [], []
        self.face_net.setInput(cv2.dnn.blobFromImage(
            image, 1.0, (300, 300), mean, False, False))
        detections = self.face_net.forward()
        for result in detections[0, 0, :, :]:
            confidence = result[2]
            if confidence > self.threshold:
                x_left_bottom = int(result[3] * cols)
                y_left_bottom = int(result[4] * rows)
                x_right_top = int(result[5] * cols)
                y_right_top = int(result[6] * rows)
                confidences.append(confidence)
                faceboxes.append(
                    [x_left_bottom, y_left_bottom, x_right_top, y_right_top])
        self.detection_result = [faceboxes, confidences]
        return confidences, faceboxes

    @staticmethod
    def draw_box(image, boxes, box_color=(255, 255, 255)):
        """Draw square boxes on image"""
        for box in boxes:
            cv2.rectangle(image,
                          (box[0], box[1]),
                          (box[2], box[3]), box_color, 3)

    @staticmethod
    def move_box(box, offset):
        """Move the box to direction specified by vector offset"""
        left_x = box[0] + offset[0]
        top_y = box[1] + offset[1]
        right_x = box[2] + offset[0]
        bottom_y = box[3] + offset[1]
        return [left_x, top_y, right_x, bottom_y]

    @staticmethod
    def get_square_box(box):
        """Get a square box out of the given box, by expanding it."""
        left_x = box[0]
        top_y = box[1]
        right_x = box[2]
        bottom_y = box[3]
        box_width = right_x - left_x
        box_height = bottom_y - top_y
        # Check if box is already a square. If not, make it a square.
        diff = box_height - box_width
        delta = int(abs(diff) / 2)
        if diff == 0:  # Already a square.
            return box
        elif diff > 0:  # Height > width, a slim box.
            left_x -= delta
            right_x += delta
            if diff % 2 == 1:
                right_x += 1
        else:  # Width > height, a short box.
            top_y -= delta
            bottom_y += delta
            if diff % 2 == 1:
                bottom_y += 1

        # Make sure box is always square.
        assert ((right_x - left_x) == (bottom_y - top_y)), 'Box is not square.'

        return [left_x, top_y, right_x, bottom_y]

    @staticmethod
    def box_in_image(box, image):
        """Check if the box is in image"""
        r = image.shape[0]  # rows
        c = image.shape[1]  # columns
        return box[0] >= 0 and box[1] >= 0 and box[2] <= c and box[3] <= r

    def extract_cnn_facebox(self, image):
        """Extract face area from image."""
        _, raw_boxes = self.get_faceboxes(image=image)
        a = []
        for box in raw_boxes:
            # Move box down.
            # diff_height_width = (box[3] - box[1]) - (box[2] - box[0])
            offset_y = int(abs((box[3] - box[1]) * 0.1))
            box_moved = self.move_box(box, [0, offset_y])

            # Make box square.
            facebox = self.get_square_box(box_moved)

            if self.box_in_image(facebox, image):
                a.append(facebox)

        return a

    def detect_marks(self, image_np):
        """Detect marks from image"""

        # # Actual detection.
        predictions = self.model.signatures["predict"](
            tf.constant(image_np, dtype=tf.uint8))
        # Convert predictions to landmarks.
        marks = np.array(predictions['output']).flatten()[:136]
        marks = np.reshape(marks, (-1, 2))
        return marks

    def plot_face_detection_marks(self, image, axis=None, show=True):
        if plt is False:
            raise ImportError("Plotting requires matplotlib!")
        if axis is None:
            fig, axis = plt.subplots()
        face_boxes = self.extract_cnn_facebox(image)
        face_box = face_boxes[0]
        face_img = image[face_box[1]: face_box[3], face_box[0]: face_box[2]]
        face_img = cv2.resize(face_img, (128, 128))
        face_img = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
        marks = self.detect_marks([face_img])
        marks *= (face_box[2] - face_box[0])
        marks[:, 0] += face_box[0]
        marks[:, 1] += face_box[1]
        axis.imshow(image, cmap="gray")
        axis.scatter(marks[:, 0], marks[:, 1], color="red", marker=".")
        if show:
            plt.show()
