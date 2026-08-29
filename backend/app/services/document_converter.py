import base64
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import unquote_to_bytes

import fitz
from bs4 import BeautifulSoup
from markitdown import MarkItDown
from PIL import Image

from app.gpt.gpt_factory import GPTFactory
from app.models.model_config import ModelConfig
from app.services.provider import ProviderService
from app.services.task_serial_executor import task_serial_executor


class DocumentConversionError(ValueError):
    pass


@dataclass
class DocumentConversionResult:
    markdown: str
    assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed_files: list[dict[str, str]] = field(default_factory=list)


class DocumentConverter:
    """Secure local-file MarkItDown adapter with BiliNote media and OCR support."""

    supported_extensions = {
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub",
        ".html", ".htm", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
        ".txt", ".md", ".ipynb", ".msg", ".zip", ".png", ".jpg", ".jpeg",
        ".gif", ".webp", ".bmp", ".tiff",
    }
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
    media_extensions = image_extensions | {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm", ".avi"}
    archive_markers = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
        ".epub": "META-INF/container.xml",
    }
    max_archive_depth = 5
    max_archive_entries = 1000
    max_archive_uncompressed_size = 200 * 1024 * 1024

    def __init__(self, task_id: str, source_path: Path, ocr_mode: str = "offline_first",
                 model_name: Optional[str] = None, provider_id: Optional[str] = None,
                 archive_depth: int = 0, archive_budget: Optional[dict] = None):
        self.task_id = task_id
        self.source_path = source_path
        self.ocr_mode = ocr_mode
        self.model_name = model_name
        self.provider_id = provider_id
        self.asset_dir = Path("static") / "document_assets" / task_id
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self._ocr_engine = None
        self._asset_counter = 0
        self.archive_depth = archive_depth
        self.archive_budget = archive_budget or {"entries": 0, "size": 0}
        self.result = DocumentConversionResult(markdown="")

    @classmethod
    def validate_source(cls, source_path: Path) -> None:
        extension = source_path.suffix.lower()
        if extension not in cls.supported_extensions:
            raise DocumentConversionError("不支持的文件格式")
        if not source_path.is_file() or source_path.stat().st_size == 0:
            raise DocumentConversionError("不能转换空文件")
        with source_path.open("rb") as source_file:
            header = source_file.read(16)
        if extension == ".pdf" and not header.startswith(b"%PDF-"):
            raise DocumentConversionError("文件内容与 PDF 格式不匹配")
        if extension in cls.archive_markers or extension == ".zip":
            if not zipfile.is_zipfile(source_path):
                raise DocumentConversionError("文件内容与扩展名不匹配")
            if extension in cls.archive_markers:
                with zipfile.ZipFile(source_path) as archive:
                    if cls.archive_markers[extension] not in archive.namelist():
                        raise DocumentConversionError("文件内容与扩展名不匹配")
        if extension in cls.image_extensions:
            try:
                with Image.open(source_path) as image:
                    image.verify()
            except Exception as exc:
                raise DocumentConversionError("无效的图片文件") from exc

    def convert(self) -> DocumentConversionResult:
        task_serial_executor.raise_if_cancelled(self.task_id)
        self.validate_source(self.source_path)
        if self.source_path.suffix.lower() == ".zip":
            markdown = self._convert_zip()
        else:
            markdown = self._convert_with_markitdown(self.source_path)
            markdown = self._append_media_and_ocr(markdown, self.source_path)
        task_serial_executor.raise_if_cancelled(self.task_id)
        if not markdown.strip():
            raise DocumentConversionError("文档未提取到可转换的内容")
        self.result.markdown = markdown.strip() + "\n"
        return self.result

    def _convert_with_markitdown(self, source_path: Path) -> str:
        try:
            # Plugins may load arbitrary third-party code. Local conversion is the only allowed input path.
            result = MarkItDown(enable_plugins=False).convert_local(source_path)
            task_serial_executor.raise_if_cancelled(self.task_id)
            return (result.markdown or "").strip()
        except Exception as exc:
            if task_serial_executor.is_cancelled(self.task_id):
                task_serial_executor.raise_if_cancelled(self.task_id)
            converted = self._convert_legacy_office(source_path)
            if converted is not None:
                return self._convert_with_markitdown(converted)
            raise DocumentConversionError(f"无法转换 {source_path.name}: {exc}") from exc

    def _convert_legacy_office(self, source_path: Path) -> Optional[Path]:
        target_extension = {".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx"}.get(source_path.suffix.lower())
        if not target_extension:
            return None
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            self.result.warnings.append(f"{source_path.name} 无法解析；安装 LibreOffice 后可转换旧版 Office 文件")
            return None
        output_dir = source_path.parent / "converted"
        output_dir.mkdir(exist_ok=True)
        try:
            subprocess.run([soffice, "--headless", "--convert-to", target_extension.lstrip("."), "--outdir", str(output_dir), str(source_path)],
                           check=True, capture_output=True, text=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        converted = output_dir / f"{source_path.stem}{target_extension}"
        return converted if converted.exists() else None

    def _convert_zip(self) -> str:
        if self.archive_depth >= self.max_archive_depth:
            raise DocumentConversionError("压缩包嵌套层数不能超过 5 层")
        sections = [f"# {self.source_path.stem}"]
        with zipfile.ZipFile(self.source_path) as archive, tempfile.TemporaryDirectory(prefix="bilinote-zip-") as directory:
            for member in archive.infolist():
                task_serial_executor.raise_if_cancelled(self.task_id)
                if member.is_dir():
                    continue
                self._validate_archive_member(member)
                extension = Path(member.filename).suffix.lower()
                if extension not in self.supported_extensions:
                    continue
                safe_name = Path(member.filename).name or "file"
                target = Path(directory) / f"{self.archive_budget['entries']:04d}-{safe_name}"
                target.write_bytes(archive.read(member))
                try:
                    child = DocumentConverter(self.task_id, target, self.ocr_mode, self.model_name, self.provider_id,
                                              self.archive_depth + 1, self.archive_budget)
                    converted = child.convert()
                    self.result.assets.extend(converted.assets)
                    self.result.warnings.extend(converted.warnings)
                    self.result.failed_files.extend(converted.failed_files)
                    sections.extend([f"## 文件：{member.filename}", converted.markdown.strip()])
                except DocumentConversionError as exc:
                    self.result.failed_files.append({"file": member.filename, "reason": str(exc)})
        if self.result.failed_files:
            failures = "\n".join(f"- `{item['file']}`：{item['reason']}" for item in self.result.failed_files)
            sections.extend(["## 未转换文件", failures])
        return "\n\n".join(sections)

    def _validate_archive_member(self, member: zipfile.ZipInfo) -> None:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise DocumentConversionError("压缩包包含不安全路径")
        if member.flag_bits & 0x1:
            raise DocumentConversionError("不支持加密压缩包")
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise DocumentConversionError("压缩包不能包含符号链接")
        if self.archive_budget["entries"] >= self.max_archive_entries:
            raise DocumentConversionError("压缩包文件数量超过 1000")
        if self.archive_budget["size"] + member.file_size > self.max_archive_uncompressed_size:
            raise DocumentConversionError("压缩包解压后不能超过 200MB")
        self.archive_budget["size"] += member.file_size
        self.archive_budget["entries"] += 1

    def _append_media_and_ocr(self, markdown: str, source_path: Path) -> str:
        extension = source_path.suffix.lower()
        media_sections: list[str] = []
        if extension == ".pdf":
            media_sections.extend(self._extract_pdf_media(source_path, markdown))
        elif extension in {".docx", ".pptx", ".xlsx", ".epub"}:
            media_sections.extend(self._extract_package_media(source_path, markdown))
        elif extension in self.image_extensions:
            asset = self._save_asset(source_path.read_bytes(), source_path.name)
            media_sections.append(f"![{source_path.name}]({asset})")
            ocr_text = self._ocr(source_path.read_bytes(), source_path.name)
            if ocr_text:
                media_sections.append(ocr_text)
        elif extension in {".html", ".htm"}:
            media_sections.extend(self._extract_html_media(source_path, markdown))
        return "\n\n".join(part for part in [markdown, *media_sections] if part.strip())

    def _extract_pdf_media(self, source_path: Path, markdown: str) -> list[str]:
        parts = []
        with fitz.open(source_path) as pdf:
            for page_number, page in enumerate(pdf, start=1):
                task_serial_executor.raise_if_cancelled(self.task_id)
                for image_number, image in enumerate(page.get_images(full=True), start=1):
                    image_data = pdf.extract_image(image[0])
                    filename = f"page-{page_number}-image-{image_number}.{image_data.get('ext', 'png')}"
                    asset = self._save_asset(image_data["image"], filename)
                    parts.append(f"![{filename}]({asset})")
                if len(re.sub(r"\s+", "", page.get_text("text"))) < 20:
                    page_bytes = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
                    ocr_text = self._ocr(page_bytes, f"page-{page_number}.png")
                    if ocr_text:
                        parts.append(ocr_text)
        return parts

    def _extract_package_media(self, source_path: Path, markdown: str) -> list[str]:
        parts = []
        with zipfile.ZipFile(source_path) as archive:
            for member in archive.namelist():
                task_serial_executor.raise_if_cancelled(self.task_id)
                extension = Path(member).suffix.lower()
                if extension not in self.media_extensions or member.endswith("/"):
                    continue
                content = archive.read(member)
                asset = self._save_asset(content, Path(member).name)
                markdown_url = f"![{Path(member).name}]({asset})" if extension in self.image_extensions else f"[附件：{Path(member).name}]({asset})"
                parts.append(markdown_url)
                if extension in self.image_extensions:
                    ocr_text = self._ocr(content, Path(member).name)
                    if ocr_text:
                        parts.append(ocr_text)
        return parts

    def _extract_html_media(self, source_path: Path, markdown: str) -> list[str]:
        parts = []
        soup = BeautifulSoup(source_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for element in soup.select("img[src], audio[src], video[src], source[src]"):
            task_serial_executor.raise_if_cancelled(self.task_id)
            reference = element.get("src", "")
            if reference.startswith("data:"):
                try:
                    header, payload = reference.split(",", 1)
                    extension = mimetypes.guess_extension(header.split(";")[0][5:]) or ".bin"
                    content = base64.b64decode(payload) if ";base64" in header else unquote_to_bytes(payload)
                except (ValueError, TypeError):
                    continue
                filename = f"embedded{extension}"
            elif not re.match(r"^[a-z]+://", reference, re.I):
                candidate = (source_path.parent / reference).resolve()
                if source_path.parent.resolve() not in candidate.parents or not candidate.is_file():
                    continue
                content, filename = candidate.read_bytes(), candidate.name
            else:
                continue
            asset = self._save_asset(content, filename)
            parts.append(f"![{filename}]({asset})" if Path(filename).suffix.lower() in self.image_extensions else f"[附件：{filename}]({asset})")
        return parts

    def _save_asset(self, content: bytes, original_name: str) -> str:
        self._asset_counter += 1
        safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(original_name).stem)[:60] or "asset"
        extension = re.sub(r"[^a-zA-Z0-9]", "", Path(original_name).suffix.lower()) or "bin"
        filename = f"{self._asset_counter:03d}-{safe_stem}.{extension}"
        (self.asset_dir / filename).write_bytes(content)
        url = f"/static/document_assets/{self.task_id}/{filename}"
        self.result.assets.append(url)
        return url

    def _ocr(self, image_bytes: bytes, name: str) -> str:
        task_serial_executor.raise_if_cancelled(self.task_id)
        if self.ocr_mode == "off":
            return ""
        offline_error = None
        if self.ocr_mode in {"offline_first", "offline_only"}:
            try:
                text = self._offline_ocr(image_bytes)
                task_serial_executor.raise_if_cancelled(self.task_id)
                if text:
                    return text
            except Exception as exc:
                offline_error = exc
        if self.ocr_mode in {"offline_first", "visual_fallback"} and self.model_name and self.provider_id:
            text = self._visual_ocr(image_bytes, name)
            task_serial_executor.raise_if_cancelled(self.task_id)
            return text
        if self.ocr_mode == "offline_only" or (offline_error and not self.model_name):
            raise DocumentConversionError("本地 OCR 不可用或未识别到文字；请安装 rapidocr-onnxruntime 或启用视觉模型兜底") from offline_error
        return ""

    def _offline_ocr(self, image_bytes: bytes) -> str:
        if self._ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr_engine = RapidOCR()
        result, _ = self._ocr_engine(image_bytes)
        return "\n".join(item[1] for item in result or [] if len(item) > 1 and item[1].strip())

    def _visual_ocr(self, image_bytes: bytes, name: str) -> str:
        provider = ProviderService.get_provider_by_id(self.provider_id)
        if not provider:
            raise DocumentConversionError("未找到视觉模型提供者")
        config = ModelConfig(api_key=provider["api_key"], base_url=provider["base_url"], model_name=self.model_name,
                             provider=provider["type"], name=provider["name"])
        gpt = GPTFactory.from_config(config)
        mime_type = mimetypes.guess_type(name)[0] or "image/png"
        image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        response = gpt.client.chat.completions.create(model=gpt.model, temperature=0, messages=[{"role": "user", "content": [
            {"type": "text", "text": "请准确识别图片中的文字，只输出可读的 Markdown 文本。"},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
        ]}])
        return (response.choices[0].message.content or "").strip()
