"""Private message images live outside Flask's publicly served static folder."""
import os
import secrets
import warnings
from io import BytesIO

from flask import current_app, request
from PIL import Image, ImageOps, UnidentifiedImageError

from . import db
from .models import MessageImage

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


def image_folder():
    return current_app.config.get(
        "MESSAGE_IMAGE_FOLDER", os.path.join(current_app.root_path, "message_images")
    )


def limit_message_upload():
    # Apply only to message submissions; leave existing large media uploads alone.
    request.max_content_length = MAX_IMAGE_BYTES + 1024 * 1024


def save_message(message):
    """Validate optional image, then commit both records; clean up on failure."""
    upload = request.files.get("image")
    output = None
    if upload and upload.filename:
        data = upload.stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Please choose an image no larger than 5 MB.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as source:
                    if source.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                        raise ValueError("Please choose a JPEG, PNG, GIF or WebP image.")
                    if source.width * source.height > MAX_IMAGE_PIXELS:
                        raise ValueError("Please choose an image with at most 16 million pixels.")
                    source.load()
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((2400, 2400))
                    # Re-encoding removes metadata and any appended file content.
                    output = BytesIO()
                    image.convert("RGBA").save(output, format="WEBP", quality=85)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            raise ValueError("This image could not be read. Please choose another image.") from exc
    if not message.body and output is None:
        raise ValueError("Add a message or choose an image.")

    path = None
    try:
        if output is not None:
            os.makedirs(image_folder(), exist_ok=True)
            filename = secrets.token_hex(24) + ".webp"
            path = os.path.join(image_folder(), filename)
            with open(path, "xb") as target:
                target.write(output.getvalue())
            message.attachment = MessageImage(filename=filename)
        db.session.add(message)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise
