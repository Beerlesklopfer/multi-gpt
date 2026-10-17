from pathlib import PurePath

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_http_methods

from multigpt.chat import svg_clean

from .models import ScratchFile


@require_GET
@login_required
def index(request):
    files = ScratchFile.objects.filter(owner=request.user)
    page = Paginator(files, 30).get_page(request.GET.get("page"))
    return render(request, "scratchpad/index.html", {"page": page})


@require_GET
@login_required
def file(request, pk):
    item = get_object_or_404(ScratchFile, pk=pk, owner=request.user)
    download = request.GET.get("download") == "1"
    mime = item.mime_type if item.is_image else "application/octet-stream"
    try:
        handle = item.file.open("rb")
        if mime == "image/svg+xml":
            with handle:
                data, _, _ = svg_clean.clean(handle.read(svg_clean.MAX_BYTES + 1))
            response = HttpResponse(data, content_type=mime)
        else:
            response = FileResponse(handle, content_type=mime)
    except (OSError, ValueError, svg_clean.SvgError) as exc:
        raise Http404 from exc
    name = PurePath(item.name.replace("\\", "/")).name
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127) or "datei"
    response["Content-Disposition"] = content_disposition_header(
        download or not item.is_image, name
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


@require_http_methods(["GET", "POST"])
@login_required
def delete(request, pk):
    item = get_object_or_404(ScratchFile, pk=pk, owner=request.user)
    if request.method == "POST" and request.POST.get("confirm") == "yes":
        item.delete()
        messages.success(request, "Scratchpad-Datei gelöscht.")
        return redirect("scratchpad:index")
    return render(
        request,
        "scratchpad/delete.html",
        {"item": item},
        status=400 if request.method == "POST" else 200,
    )
