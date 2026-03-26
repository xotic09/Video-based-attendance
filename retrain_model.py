import argparse
import os
import shutil
import time

from classifier import training
from preprocess import preprocesses


INPUT_DATADIR = "./train_img"
OUTPUT_DATADIR = "./aligned_img"
MODELDIR = "./model/20180402-114759.pb"
CLASSIFIER_FILENAME = "./class/classifier.pkl"


def remove_aligned_outputs(output_dir):
    if not os.path.isdir(output_dir):
        return

    for entry in os.listdir(output_dir):
        entry_path = os.path.join(output_dir, entry)
        if os.path.isdir(entry_path):
            try:
                shutil.rmtree(entry_path)
            except PermissionError:
                print("Warning: could not remove %s (locked), skipping" % entry_path)
        elif entry.startswith("bounding_boxes_") and entry.endswith(".txt"):
            for attempt in range(3):
                try:
                    os.remove(entry_path)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(1)
                    else:
                        print("Warning: could not delete %s (file locked), skipping" % entry_path)


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild aligned face images and retrain the attendance classifier."
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete existing aligned images before preprocessing.",
    )
    args = parser.parse_args()

    if args.clean:
        print("Removing existing aligned images...")
        remove_aligned_outputs(OUTPUT_DATADIR)

    print("Starting preprocessing...")
    preprocess = preprocesses(INPUT_DATADIR, OUTPUT_DATADIR)
    stats = preprocess.collect_data()
    print("Preprocessing summary")
    print("  Total images: %d" % stats["total_images"])
    print("  Newly aligned: %d" % stats["newly_aligned"])
    print("  Skipped existing: %d" % stats["skipped_existing"])
    print("  Failed alignments: %d" % stats["failed"])

    print("Starting classifier training...")
    trainer = training(OUTPUT_DATADIR, MODELDIR, CLASSIFIER_FILENAME)
    classifier_file = trainer.main_train()
    print('Saved classifier model to file "%s"' % classifier_file)
    print("Retraining complete")


if __name__ == "__main__":
    main()
