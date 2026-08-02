import os
import uuid

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.files.storage import default_storage
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_protect
from filer.models import Image as FilerImage
from PIL import Image

#: Conservative default; overridden by settings.TINYMCE_UPLOAD_MAX_BYTES.
TINYMCE_UPLOAD_MAX_BYTES = getattr(settings, "TINYMCE_UPLOAD_MAX_BYTES", 5 * 1024 * 1024)

#: Real, Pillow-verified image formats accepted from editors - deliberately
#: excludes SVG/HTML/XML/PDF and any format that cannot be safely decoded as
#: a raster image. The stored file's extension is derived from this, never
#: from the uploaded file's declared name or content type.
ALLOWED_UPLOAD_FORMATS = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
}


def _detect_safe_extension(f) -> str | None:
    """
    Returns the safe extension for `f` if it is a real, fully decodable
    image in an allowed format, or None otherwise. Never trusts the
    declared content_type or the original filename's extension.
    """
    try:
        image = Image.open(f)
        image.verify()
        # Pillow requires re-opening after verify() - the file object is
        # left unusable for further decoding once verify() has run.
        f.seek(0)
        image = Image.open(f)
        image.load()
    except Exception:  # noqa: BLE001 - any failure to open/decode means "not a valid image"
        return None
    return ALLOWED_UPLOAD_FORMATS.get(image.format)


@staff_member_required
@csrf_protect
def tinymce_upload(request: HttpRequest):
    if request.method != "POST" or "file" not in request.FILES:
        return JsonResponse({"error": "Invalid request"}, status=400)

    f = request.FILES["file"]

    if f.size > TINYMCE_UPLOAD_MAX_BYTES:
        return JsonResponse({"error": "File too large"}, status=400)

    extension = _detect_safe_extension(f)
    if not extension:
        return JsonResponse({"error": "Invalid image file"}, status=400)

    f.seek(0)
    name = f"{uuid.uuid4().hex}.{extension}"
    rel_path = os.path.join("tinymce", name)
    saved_path = default_storage.save(rel_path, f)
    url = default_storage.url(saved_path)
    return JsonResponse({"location": url})


@staff_member_required
def tinymce_image_list(request):
    items = []
    for img in FilerImage.objects.order_by("-modified_at")[:200]:
        items.append({"title": img.label or img.original_filename, "value": img.url})
    return JsonResponse(items, safe=False)
