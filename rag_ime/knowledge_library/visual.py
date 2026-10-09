"""Bounded local visual evidence; original sources remain the citation authority."""
from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import DocumentParseError, ParsedAsset, ParsedBlock, ParsedDocument


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"})
IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"})
MAX_VISUAL_UNITS = 64
MAX_RENDER_SIDE = 1024
MAX_IMAGE_PIXELS = 20_000_000


def parse_native_image(path: Path) -> ParsedDocument:
    try:
        from PIL import Image
        with Image.open(path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise DocumentParseError("image exceeds native embedding pixel limit", code="image_too_large")
            image.verify()
    except ImportError as exc:
        raise DocumentParseError("native image embedding requires embedding-local dependencies", code="visual_dependencies_missing") from exc
    except (OSError, ValueError) as exc:
        raise DocumentParseError("native image input is invalid", code="invalid_image") from exc
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    asset = ParsedAsset(path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream", digest, payload)
    return ParsedDocument(
        text=f"[Image source: {path.name}]", provider="builtin", provider_version="native-image-v1",
        assets=(asset,), blocks=(ParsedBlock("image", "", metadata={"assetSha256": digest, "assetName": path.name, "ocrApplied": False}),),
        metadata={"textSource": "source-label", "ocrApplied": False},
    )


def attach_visual_evidence(parsed: ParsedDocument, path: Path) -> ParsedDocument:
    """Render PDF pages and retain existing local package images for image encoding."""
    assets = list(parsed.assets)
    blocks = list(parsed.blocks)
    total_pages = 0
    if path.suffix.lower() == ".pdf":
        try:
            import pymupdf
        except ImportError as exc:
            raise DocumentParseError("native PDF embedding requires embedding-local dependencies", code="visual_dependencies_missing") from exc
        try:
            with pymupdf.open(path) as pdf:
                if pdf.needs_pass:
                    raise DocumentParseError("PDF is encrypted", code="pdf_encrypted")
                total_pages = len(pdf)
                for index in range(min(total_pages, MAX_VISUAL_UNITS)):
                    page = pdf[index]
                    side = max(page.rect.width, page.rect.height)
                    if side <= 0:
                        raise DocumentParseError("PDF page has invalid dimensions", code="invalid_pdf")
                    scale = min(2.0, MAX_RENDER_SIDE / side)
                    payload = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
                    digest = hashlib.sha256(payload).hexdigest()
                    name = f"page-{index + 1}.png"
                    assets.append(ParsedAsset(name, "image/png", digest, payload))
                    blocks.append(ParsedBlock("image", "", page=index + 1, metadata={
                        "assetSha256": digest, "assetName": name, "sourcePart": "pdf-page-render", "ocrApplied": False,
                    }))
        except DocumentParseError:
            raise
        except Exception as exc:
            raise DocumentParseError("PDF visual rendering failed", code="invalid_pdf") from exc
    referenced = {block.metadata.get("assetSha256") for block in blocks if block.kind in {"image", "figure"}}
    for asset in assets:
        if asset.media_type in IMAGE_MIME_TYPES and asset.sha256 not in referenced:
            blocks.append(ParsedBlock("image", "", metadata={"assetSha256": asset.sha256, "assetName": asset.name, "ocrApplied": False}))
            referenced.add(asset.sha256)
    supported_hashes = {asset.sha256 for asset in assets if asset.media_type in IMAGE_MIME_TYPES}
    blocks = [replace(block, metadata={**block.metadata, "nativeImageEmbedding": block.metadata.get("assetSha256") in supported_hashes})
        if block.kind in {"image", "figure"} else block for block in blocks]
    visual_count = len({(block.metadata.get("assetSha256"), block.page) for block in blocks
        if block.metadata.get("nativeImageEmbedding") is True})
    return replace(parsed, assets=tuple(assets), blocks=tuple(blocks), metadata={
        **parsed.metadata, "nativeImageEmbeddings": True, "visualUnitLimit": MAX_VISUAL_UNITS,
        "visualPageCount": total_pages, "visualPagesTruncated": total_pages > MAX_VISUAL_UNITS,
        "visualUnitCount": visual_count, "visualUnitsTruncated": visual_count > MAX_VISUAL_UNITS,
    })


def visual_spans(parsed: ParsedDocument) -> list[dict[str, Any]]:
    if not parsed.metadata.get("nativeImageEmbeddings"):
        return []
    spans = []
    seen = set()
    for order, block in enumerate(parsed.blocks):
        digest = block.metadata.get("assetSha256")
        if block.kind not in {"image", "figure"} or block.metadata.get("nativeImageEmbedding") is not True or not isinstance(digest, str) or len(digest) != 64:
            continue
        identity = (digest, block.page)
        if identity in seen:
            continue
        seen.add(identity)
        label = f"[Visual source: page {block.page}]" if block.page else "[Visual source: image asset]"
        metadata = {key: block.metadata[key] for key in ("assetSha256", "assetName", "sourcePart", "ocrApplied") if key in block.metadata}
        spans.append({"content": label, "heading": " > ".join(block.heading_path), "page": block.page, "provenance": {
            "kind": "image", "modality": "image", "assetSha256": digest,
            "parser": parsed.provider, "parserVersion": parsed.provider_version,
            "sourceBlocks": [{"id": hashlib.sha256(f"visual:{digest}:{block.page}".encode()).hexdigest()[:24],
                "order": order, "kind": "image", "page": block.page,
                "bbox": list(block.bbox) if block.bbox is not None else None, "metadata": metadata}],
        }})
        if len(spans) >= MAX_VISUAL_UNITS:
            break
    return spans
