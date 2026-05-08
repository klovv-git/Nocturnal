#!/usr/bin/env python3
"""
train_yolo_sar.py — NOCTURNAL Phase 3: fine-tune YOLOv8 on SAR ship data.

Trains YOLOv8n starting from the ImageNet-pretrained weights, fine-tuning
on the SAR ship detection dataset so the model learns what ships look like
in radar imagery rather than natural photographs.

Usage:
    python train_yolo_sar.py

Trained weights will be saved to:
    runs/nocturnal/yolov8n-sar-finetune/weights/best.pt

Use that best.pt as --weights in yolo_infer_sar.py.
"""

from pathlib import Path

try:
    from ultralytics import YOLO
except ImportError:
    raise SystemExit("Missing ultralytics. Run: pip install ultralytics")

DATASET_YAML = Path("SAR Ship Data Detection.v1i.yolov8") / "data.yaml"
BASE_WEIGHTS  = "yolov8n.pt"   # pretrained starting point
PROJECT       = "runs/nocturnal"
RUN_NAME      = "yolov8n-sar-finetune"

if __name__ == "__main__":
    if not DATASET_YAML.exists():
        raise SystemExit(f"Dataset not found at: {DATASET_YAML}\n"
                         "Unzip the Roboflow download into the Nocturnal folder first.")

    print(f"[train] dataset : {DATASET_YAML}")
    print(f"[train] base    : {BASE_WEIGHTS}")
    print(f"[train] output  : {PROJECT}/{RUN_NAME}/weights/best.pt")
    print()

    model = YOLO(BASE_WEIGHTS)

    results = model.train(
        data=str(DATASET_YAML),
        epochs=50,
        imgsz=800,      # matches our tile size exactly
        batch=16,       # safe for 10 GB VRAM with YOLOv8n at 800px
        device=0,       # GPU 0
        project=PROJECT,
        name=RUN_NAME,
        exist_ok=True,  # overwrite if run already exists
        patience=10,    # stop early if no improvement for 10 epochs
        save=True,
        plots=True,
        workers=0,  # disable multiprocessing on Windows
    )

    best = Path(PROJECT) / RUN_NAME / "weights" / "best.pt"
    print()
    print(f"[done] Best weights: {best}")
    print()
    print("Next step — run inference with the fine-tuned model:")
    print(f'  python yolo_infer_sar.py <SAFE> --weights "{best}" --db ais_memory.db --conf 0.25')
