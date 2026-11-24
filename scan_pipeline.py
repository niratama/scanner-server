import datetime
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

import img2pdf
from PIL import Image, ImageEnhance

from drive_client import upload_pdf

JOBS_ROOT = Path("/tmp/dr_c125_jobs")

BLANK_SENSITIVITY_MAP: dict[str, tuple[int, float]] = {
    "low": (200, 0.0010),
    "normal": (215, 0.0025),
    "high": (225, 0.0060),
}

ROTATION_TRANSPOSE_MAP = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def is_blank_page(
    img_path: Path,
    dark_threshold: int = 215,
    dark_ratio_limit: float = 0.0025,
) -> bool:
    """縮小グレースケールのヒストグラムから白紙判定する（高速・省メモリ）。"""
    with Image.open(img_path) as img:
        gray = img.convert("L")
        gray.thumbnail((400, 400))
        hist = gray.histogram()
        total_pixels = sum(hist)
        if total_pixels == 0:
            return True
        dark_pixels = sum(hist[:dark_threshold])
        return (dark_pixels / total_pixels) < dark_ratio_limit


def sanitize_filename(name: str) -> str:
    """ファイル名に使えない文字を除去し、拡張子 .pdf を整える。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name.strip())
    if not cleaned:
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        cleaned = f"scan_{now}"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def build_scanimage_cmd(
    *,
    batch_pattern: str,
    resolution: int = 300,
    mode: str = "Color",
    duplex: bool = True,
    swdeskew: bool = True,
    rollerdeskew: bool = True,
    swcrop: bool = True,
    df_length: bool = True,
    stapledetect: bool = False,
    buffermode: bool = True,
) -> list[str]:
    """DR-C125 実機検証済み仕様に基づき scanimage コマンドを組み立てる。"""
    source = "ADF Duplex" if duplex else "ADF Front"
    img_format = "png" if mode == "Lineart" else "jpeg"
    return [
        "scanimage",
        f"--source={source}",
        f"--mode={mode}",
        f"--resolution={resolution}",
        "--page-width=215.872",
        "--page-height=297",
        "-x",
        "215.872",
        "-y",
        "297",
        f"--swdeskew={_yes_no(swdeskew)}",
        f"--rollerdeskew={_yes_no(rollerdeskew)}",
        f"--swcrop={_yes_no(swcrop)}",
        f"--df-length={_yes_no(df_length)}",
        f"--stapledetect={_yes_no(stapledetect)}",
        f"--buffermode={_yes_no(buffermode)}",
        f"--format={img_format}",
        f"--batch={batch_pattern}",
    ]


def process_page_image(
    img_path: Path,
    *,
    rotation: int = 0,
    brightness: int = 0,
    contrast: int = 0,
    jpeg_quality: int = 0,
    mode: str = "Color",
) -> Path:
    """必要な場合のみ 1 ページずつ回転・明るさ/コントラスト・JPEG 圧縮を適用する。"""
    is_lineart = mode == "Lineart"
    needs_tone = (not is_lineart) and (brightness != 0 or contrast != 0)
    needs_quality = (not is_lineart) and (jpeg_quality > 0)
    needs_rotate = rotation in ROTATION_TRANSPOSE_MAP

    if not (needs_rotate or needs_tone or needs_quality):
        return img_path

    out_path = img_path.with_name(f"proc_{img_path.name}")
    with Image.open(img_path) as img:
        dpi = img.info.get("dpi")
        processed = img.copy()

    if needs_rotate:
        processed = processed.transpose(ROTATION_TRANSPOSE_MAP[rotation])

    if needs_tone:
        if processed.mode not in ("RGB", "L"):
            processed = processed.convert("RGB")
        if brightness != 0:
            factor = max(0.2, 1.0 + brightness / 100.0)
            processed = ImageEnhance.Brightness(processed).enhance(factor)
        if contrast != 0:
            factor = max(0.2, 1.0 + contrast / 100.0)
            processed = ImageEnhance.Contrast(processed).enhance(factor)

    save_kwargs: dict = {}
    if dpi:
        save_kwargs["dpi"] = dpi

    if is_lineart:
        processed.save(out_path, format="PNG", optimize=True, **save_kwargs)
    else:
        if processed.mode not in ("RGB", "L"):
            processed = processed.convert("RGB")
        quality = jpeg_quality if jpeg_quality > 0 else 85
        processed.save(
            out_path,
            format="JPEG",
            quality=quality,
            optimize=True,
            **save_kwargs,
        )

    return out_path


def run_scan_job(
    *,
    resolution: int = 300,
    mode: str = "Color",
    duplex: bool = True,
    skip_blank: bool = True,
    blank_sensitivity: str = "normal",
    swdeskew: bool = True,
    rollerdeskew: bool = True,
    swcrop: bool = True,
    df_length: bool = True,
    stapledetect: bool = False,
    buffermode: bool = True,
    rotation: int = 0,
    jpeg_quality: int = 0,
    brightness: int = 0,
    contrast: int = 0,
    subfolder: str = "",
    filename: str = "",
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """DR-C125 でスキャンし、白紙除去→画像調整→PDF化→Google Drive 保存まで実行する。"""

    def report(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    job_dir = JOBS_ROOT / f"job_{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        ext = "png" if mode == "Lineart" else "jpg"
        batch_pattern = str(job_dir / f"page_%04d.{ext}")
        cmd = build_scanimage_cmd(
            batch_pattern=batch_pattern,
            resolution=resolution,
            mode=mode,
            duplex=duplex,
            swdeskew=swdeskew,
            rollerdeskew=rollerdeskew,
            swcrop=swcrop,
            df_length=df_length,
            stapledetect=stapledetect,
            buffermode=buffermode,
        )

        mode_label = {"Color": "カラー", "Gray": "グレー", "Lineart": "白黒2値"}.get(mode, mode)
        report(f"スキャン中 ({resolution}dpi / {mode_label} / {'両面' if duplex else '片面'})...")
        proc = subprocess.run(cmd, capture_output=True, text=True)

        scanned_files = sorted(job_dir.glob(f"page_*.{ext}"))
        if not scanned_files:
            err = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"原稿が読み取れませんでした: {err}")

        # 白紙除去
        kept_files = []
        removed_count = 0
        if skip_blank:
            dark_threshold, dark_ratio_limit = BLANK_SENSITIVITY_MAP.get(
                blank_sensitivity, BLANK_SENSITIVITY_MAP["normal"]
            )
            report(f"白紙チェック中 ({len(scanned_files)} ページ)...")
            for p in scanned_files:
                if is_blank_page(
                    p,
                    dark_threshold=dark_threshold,
                    dark_ratio_limit=dark_ratio_limit,
                ):
                    removed_count += 1
                else:
                    kept_files.append(p)
        else:
            kept_files = scanned_files

        if not kept_files:
            raise RuntimeError(
                "すべてのページが白紙と判定されました。白紙除去をオフにするか感度を下げて再試行してください。"
            )

        # 画像調整（必要な場合のみ 1 ページずつ処理）
        final_files = []
        needs_postprocess = (
            rotation in ROTATION_TRANSPOSE_MAP
            or (mode != "Lineart" and (brightness != 0 or contrast != 0 or jpeg_quality > 0))
        )
        if needs_postprocess:
            report(f"画像調整中 ({len(kept_files)} ページ)...")
            for p in kept_files:
                final_files.append(
                    process_page_image(
                        p,
                        rotation=rotation,
                        brightness=brightness,
                        contrast=contrast,
                        jpeg_quality=jpeg_quality,
                        mode=mode,
                    )
                )
        else:
            final_files = kept_files

        # PDF 結合（未加工時は元画像を再圧縮せず無劣化で格納）
        report(f"PDF 作成中 ({len(final_files)} ページ)...")
        pdf_name = sanitize_filename(filename)
        pdf_path = job_dir / pdf_name
        with pdf_path.open("wb") as f:
            f.write(img2pdf.convert([str(p) for p in final_files]))

        pdf_size_mb = round(pdf_path.stat().st_size / (1024 * 1024), 2)

        # Google Drive へアップロード
        report(f"Google Drive へアップロード中 ({pdf_size_mb} MB)...")
        uploaded = upload_pdf(pdf_path, upload_name=pdf_name, subfolder=subfolder)

        return {
            "name": uploaded["name"],
            "url": uploaded.get("webViewLink", ""),
            "folder": uploaded.get("folder", "DR-C125_Scans"),
            "scanned_pages": len(scanned_files),
            "kept_pages": len(final_files),
            "removed_blank_pages": removed_count,
            "size_mb": pdf_size_mb,
            "finished_at": datetime.datetime.now().strftime("%H:%M:%S"),
        }
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
