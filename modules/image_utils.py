"""Shared image prep for vision calls (Dispatch screenshots, Winston report
photos). Full-resolution phone-camera uploads are commonly 3000px+ on the
long edge and several MB — that bulk is pure base64/upload/decode latency
for a vision model that only needs to read text, so every vision call
downscales first."""
import io

from PIL import Image, ImageOps


def downscale_for_vision(image_bytes, content_type="image/jpeg", max_dim=1600, quality=82):
    """Resize (if needed) and re-encode as JPEG. Returns (bytes, content_type).
    Falls back to the original bytes/content_type if Pillow can't decode the
    image (e.g. an unusual format) rather than failing the whole parse."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # mobile photos carry rotation in EXIF, not pixels
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        width, height = img.size
        longest = max(width, height)
        if longest > max_dim:
            scale = max_dim / longest
            img = img.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, content_type
