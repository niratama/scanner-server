import datetime
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

import img2pdf
from PIL import Image, ImageEnhance

from drive_client import ensure_subfolder, get_drive_service, upload_file, upload_pdf

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

OUTPUT_FORMAT_META = {
    "pdf": (".pdf", "application/pdf"),
    "jpeg": (".jpg", "image/jpeg"),
    "png": (".png", "image/png"),
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


def sanitize_base_name(name: str) -> str:
    """ファイル名に使えない文字や既知の拡張子を除去し、ベース名を返す。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name.strip())
    cleaned = re.sub(r"\.(pdf|jpe?g|png)$", "", cleaned, flags=re.IGNORECASE).strip()
    if not cleaned:
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        cleaned = f"scan_{now}"
    return cleaned


def sanitize_filename(name: str, output_format: str = "pdf") -> str:
    """出力形式に応じた安全なファイル名（拡張子付き）を返す。"""
    ext, _ = OUTPUT_FORMAT_META.get(output_format, OUTPUT_FORMAT_META["pdf"])
    return f"{sanitize_base_name(name)}{ext}"


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _capture_format(mode: str, output_format: str) -> str:
    if mode == "Lineart" or output_format == "png":
        return "png"
    return "jpeg"


def build_scanimage_cmd(
    *,
    batch_pattern: str,
    resolution: int = 300,
    mode: str = "Color",
    duplex: bool = True,
    output_format: str = "pdf",
    swdeskew: bool = True,
    rollerdeskew: bool = True,
    swcrop: bool = True,
    df_length: bool = True,
    stapledetect: bool = False,
    buffermode: bool = True,
) -> list[str]:
    """DR-C125 実機検証済み仕様に基づき scanimage コマンドを組み立てる。"""
    source = "ADF Duplex" if duplex else "ADF Front"
    img_format = _capture_format(mode, output_format)
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
    output_format: str = "pdf",
) -> Path:
    """必要な場合のみ 1 ページずつ回転・明るさ/コントラスト・形式変換/圧縮を適用する。"""
    is_lineart = mode == "Lineart"
    target_is_png = output_format == "png" or (output_format == "pdf" and is_lineart)
    needs_convert = (target_is_png and img_path.suffix.lower() != ".png") or (
        (not target_is_png) and img_path.suffix.lower() not in (".jpg", ".jpeg")
    )
    needs_tone = (not is_lineart) and (brightness != 0 or contrast != 0)
    needs_quality = (not target_is_png) and (not is_lineart) and (jpeg_quality > 0)
    needs_rotate = rotation in ROTATION_TRANSPOSE_MAP

    if not (needs_convert or needs_rotate or needs_tone or needs_quality):
        return img_path

    out_ext = ".png" if target_is_png else ".jpg"
    out_path = img_path.with_name(f"proc_{img_path.stem}{out_ext}")
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

    if target_is_png:
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
    output_format: str = "pdf",
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
    trigger: str = "web",
    preset_name: str = "",
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """DR-C125 でスキャンし、白紙除去→画像調整→PDF/JPEG/PNG 出力→Google Drive 保存まで実行する。"""

    def report(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    if output_format not in OUTPUT_FORMAT_META:
        raise ValueError(f"未対応の出力形式です: {output_format}")

    job_dir = JOBS_ROOT / f"job_{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        cap_fmt = _capture_format(mode, output_format)
        ext = "png" if cap_fmt == "png" else "jpg"
        batch_pattern = str(job_dir / f"page_%04d.{ext}")
        cmd = build_scanimage_cmd(
            batch_pattern=batch_pattern,
            resolution=resolution,
            mode=mode,
            duplex=duplex,
            output_format=output_format,
            swdeskew=swdeskew,
            rollerdeskew=rollerdeskew,
            swcrop=swcrop,
            df_length=df_length,
            stapledetect=stapledetect,
            buffermode=buffermode,
        )

        mode_label = {"Color": "カラー", "Gray": "グレー", "Lineart": "白黒2値"}.get(mode, mode)
        report(
            f"スキャン中 ({resolution}dpi / {mode_label} / {'両面' if duplex else '片面'} / {output_format.upper()})..."
        )
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
        for idx, p in enumerate(kept_files, start=1):
            processed = process_page_image(
                p,
                rotation=rotation,
                brightness=brightness,
                contrast=contrast,
                jpeg_quality=jpeg_quality,
                mode=mode,
                output_format=output_format,
            )
            if processed != p:
                report(f"画像調整中 ({idx}/{len(kept_files)} ページ)...")
            final_files.append(processed)

        base_name = sanitize_base_name(filename)
        finished_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if output_format == "pdf":
            report(f"PDF 作成中 ({len(final_files)} ページ)...")
            pdf_name = f"{base_name}.pdf"
            pdf_path = job_dir / pdf_name
            with pdf_path.open("wb") as f:
                f.write(img2pdf.convert([str(p) for p in final_files]))

            total_size_mb = round(pdf_path.stat().st_size / (1024 * 1024), 2)
            report(f"Google Drive へアップロード中 ({total_size_mb} MB)...")
            uploaded = upload_pdf(pdf_path, upload_name=pdf_name, subfolder=subfolder)
            uploaded_files = [
                {"name": uploaded["name"], "url": uploaded.get("webViewLink", "")}
            ]
            folder_label = uploaded.get("folder", "DR-C125_Scans")
            display_name = uploaded["name"]
            primary_url = uploaded.get("webViewLink", "")
        else:
            out_ext, mimetype = OUTPUT_FORMAT_META[output_format]
            total_bytes = sum(p.stat().st_size for p in final_files)
            total_size_mb = round(total_bytes / (1024 * 1024), 2)

            service = get_drive_service()
            target_folder_id, folder_label = ensure_subfolder(service, subfolder)

            uploaded_files = []
            for idx, page_path in enumerate(final_files, start=1):
                if len(final_files) == 1:
                    upload_name = f"{base_name}{out_ext}"
                else:
                    upload_name = f"{base_name}_{idx:03d}{out_ext}"
                report(
                    f"Google Drive へアップロード中 ({idx}/{len(final_files)} ファイル / {total_size_mb} MB)..."
                )
                uploaded = upload_file(
                    page_path,
                    mimetype=mimetype,
                    upload_name=upload_name,
                    folder_id=target_folder_id,
                    subfolder=subfolder,
                    service=service,
                )
                uploaded_files.append(
                    {"name": uploaded["name"], "url": uploaded.get("webViewLink", "")}
                )

            primary_url = uploaded_files[0]["url"]
            if len(uploaded_files) == 1:
                display_name = uploaded_files[0]["name"]
            else:
                display_name = f"{uploaded_files[0]['name']} (+{len(uploaded_files) - 1}件)"

        return {
            "name": display_name,
            "url": primary_url,
            "files": uploaded_files,
            "file_count": len(uploaded_files),
            "output_format": output_format,
            "folder": folder_label,
            "scanned_pages": len(scanned_files),
            "kept_pages": len(final_files),
            "removed_blank_pages": removed_count,
            "size_mb": total_size_mb,
            "finished_at": finished_at,
            "trigger": trigger,
            "preset_name": preset_name,
        }
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
