from __future__ import annotations

import argparse
import json
import pickle
import shutil
from pathlib import Path

from PIL import Image


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def list_images(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.iterdir() if path.suffix.lower() in IMAGE_EXTS),
        key=lambda path: int(path.stem),
    )


def adjusted_box(box: tuple[int, int, int, int], pads: list[int], width: int, height: int) -> tuple[int, int, int, int]:
    y1, y2, x1, x2 = box
    top, bottom, left, right = pads
    y1 = max(0, min(height - 1, int(y1) - top))
    y2 = max(y1 + 1, min(height, int(y2) + bottom))
    x1 = max(0, min(width - 1, int(x1) - left))
    x2 = max(x1 + 1, min(width, int(x2) + right))
    return y1, y2, x1, x2


def rebuild(source_avatar: Path, target_avatar: Path, pads: list[int], img_size: int, overwrite: bool) -> None:
    source_full = source_avatar / "full_imgs"
    source_coords = source_avatar / "coords.pkl"
    if not source_full.exists() or not source_coords.exists():
        raise FileNotFoundError("source avatar must contain full_imgs and coords.pkl")

    if target_avatar.exists():
        if not overwrite:
            raise FileExistsError(f"target avatar already exists: {target_avatar}")
        shutil.rmtree(target_avatar)

    target_full = target_avatar / "full_imgs"
    target_face = target_avatar / "face_imgs"
    target_full.mkdir(parents=True, exist_ok=True)
    target_face.mkdir(parents=True, exist_ok=True)

    with source_coords.open("rb") as file:
        coords = pickle.load(file)

    images = list_images(source_full)
    if not images:
        raise RuntimeError(f"no images found in {source_full}")
    if len(images) != len(coords):
        raise RuntimeError(f"image count {len(images)} does not match coords count {len(coords)}")

    next_coords = []
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    for index, image_path in enumerate(images):
        target_name = f"{index:08d}.png"
        with Image.open(image_path) as image:
            image = image.convert("RGBA") if image.mode == "RGBA" else image.convert("RGB")
            width, height = image.size
            y1, y2, x1, x2 = adjusted_box(coords[index], pads, width, height)
            face = image.crop((x1, y1, x2, y2)).resize((img_size, img_size), resampling)
            image.save(target_full / target_name)
            face.save(target_face / target_name)
            next_coords.append((y1, y2, x1, x2))

    with (target_avatar / "coords.pkl").open("wb") as file:
        pickle.dump(next_coords, file)

    metadata = {
        "source_avatar": str(source_avatar),
        "generation_pads": pads,
        "baked_pads": pads,
        "img_size": img_size,
        "frames": len(images),
    }
    (target_avatar / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"rebuilt avatar: {target_avatar}")
    print(f"frames: {len(images)}")
    print(f"img_size: {img_size}")
    print(f"runtime pads baked into coords: {pads}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-avatar", required=True)
    parser.add_argument("--target-avatar", required=True)
    parser.add_argument("--runtime-pads", nargs=4, type=int, required=True, metavar=("TOP", "BOTTOM", "LEFT", "RIGHT"))
    parser.add_argument("--img-size", type=int, default=256)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    rebuild(
        source_avatar=Path(args.source_avatar),
        target_avatar=Path(args.target_avatar),
        pads=args.runtime_pads,
        img_size=args.img_size,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()
