from io import BytesIO
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

SIZE = (3840, 2160)
Image.MAX_IMAGE_PIXELS = 120_000_000
register_heif_opener(thumbnails=False)


def prepare_photo(data: bytes, rotation: int, layout: str) -> bytes:
    if rotation not in (0, 90, 180, 270):
        raise ValueError("Rotation must be 0, 90, 180, or 270.")
    if layout not in ("fit", "fill"):
        raise ValueError("Choose fit or fill.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in ("JPEG", "MPO", "PNG", "WEBP", "HEIF"):
                    raise ValueError("Choose a JPEG, PNG, WebP, HEIC, or HEIF photo.")
                source.load()
                photo = ImageOps.exif_transpose(source)
                if rotation:
                    photo = photo.rotate(-rotation, expand=True)
                rgba = photo.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "black")
                background.alpha_composite(rgba)
                rgb = background.convert("RGB")
                if layout == "fill":
                    result = ImageOps.fit(rgb, SIZE, method=Image.Resampling.LANCZOS)
                else:
                    fitted = ImageOps.contain(rgb, SIZE, method=Image.Resampling.LANCZOS)
                    result = Image.new("RGB", SIZE, "black")
                    result.paste(
                        fitted,
                        ((SIZE[0] - fitted.width) // 2, (SIZE[1] - fitted.height) // 2),
                    )
                output = BytesIO()
                result.save(output, "JPEG", quality=94, optimize=True)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError(
            "Cannot read this photo. Use a valid JPEG, PNG, WebP, HEIC, or HEIF "
            "image under 120 megapixels."
        ) from exc
