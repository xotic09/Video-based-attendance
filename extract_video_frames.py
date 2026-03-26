import argparse
import os
from pathlib import Path

import cv2


def extract_frames(video_path, output_root="extracted_images", frame_interval=1):
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    output_root = Path(output_root)
    output_folder = output_root / video_path.stem
    output_folder.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    saved_count = 0
    frame_index = 0

    try:
        while True:
            success, frame = capture.read()
            if not success:
                break

            if frame_index % frame_interval == 0:
                frame_name = output_folder / f"frame_{saved_count:05d}.jpg"
                cv2.imwrite(str(frame_name), frame)
                saved_count += 1

            frame_index += 1
    finally:
        capture.release()

    return output_folder, saved_count


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract images from a video into a separate folder."
    )
    parser.add_argument("video", help="Path to the input video file")
    parser.add_argument(
        "--output",
        default="extracted_images",
        help="Root folder where extracted images will be stored",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3,
        help="Save every Nth frame. Default: 3",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.interval < 1:
        raise ValueError("--interval must be 1 or greater")

    folder, total_saved = extract_frames(
        video_path=args.video,
        output_root=args.output,
        frame_interval=args.interval,
    )

    print(f"Saved {total_saved} image(s) to: {folder}")
