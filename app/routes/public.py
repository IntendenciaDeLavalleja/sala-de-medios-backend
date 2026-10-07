from datetime import date

from flask import Blueprint, abort, request, url_for
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..extensions import db, limiter
from ..media import album_zip, photo_response
from ..models import Category, Event, Photo
from ..security import normalize

bp = Blueprint("public", __name__, url_prefix="/api/public")


def photo_json(photo):
    return {"id": photo.id, "alt": photo.alt, "width": photo.width, "height": photo.height,
            "bytes": photo.size_bytes, "content_type": photo.content_type, "credits": photo.credits,
            "source_url": photo.source_url, "is_cover": photo.is_cover,
            "preview_url": url_for("public.photo_content", photo_id=photo.id, variant="preview"),
            "url": url_for("public.photo_content", photo_id=photo.id),
            "download_url": url_for("public.photo_content", photo_id=photo.id, download=1)}


def event_json(event, detail=False):
    cover = next((p for p in event.photos if p.is_cover), next(iter(event.photos), None))
    result = {"id": event.id, "slug": event.slug, "title": event.title, "summary": event.summary,
              "date": event.event_date.isoformat(), "location": event.location,
              "category": {"id": event.category.id, "name": event.category.name},
              "cover": photo_json(cover) if cover else None, "cover_position": event.cover_position,
              "photo_count": len(event.photos)}
    if detail:
        result.update(description=event.description, source_url=event.source_url, credits=event.credits,
                      usage_terms=event.usage_terms, photos=[photo_json(p) for p in event.photos],
                      download_url=url_for("public.album_download", slug=event.slug))
    return result


@bp.get("/events")
def catalog():
    query = Event.query.filter_by(published=True).options(joinedload(Event.category))
    terms = normalize(request.args.get("q", "")[:200]).split()
    for term in terms[:10]:
        query = query.filter(Event.search_text.contains(term, autoescape=True))
    category = request.args.get("category", type=int)
    if category:
        query = query.filter_by(category_id=category)
    dates = []
    for parameter, comparator in (("from", Event.event_date.__ge__), ("to", Event.event_date.__le__)):
        value = request.args.get(parameter)
        if value:
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                abort(400)
            query = query.filter(comparator(parsed))
            dates.append(parsed)
    if len(dates) == 2 and dates[0] > dates[1]:
        abort(400)
    sort = request.args.get("sort", "newest")
    orders = {"newest": (Event.event_date.desc(), Event.id.desc()),
              "oldest": (Event.event_date.asc(), Event.id.asc()), "title": (Event.title.asc(), Event.id.asc())}
    if sort not in orders:
        abort(400)
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(30, max(1, request.args.get("per_page", 6, type=int)))
    pagination = query.order_by(*orders[sort]).paginate(page=page, per_page=per_page, error_out=False)
    return {"items": [event_json(event) for event in pagination.items], "total": pagination.total,
            "page": page, "pages": pagination.pages, "has_next": pagination.has_next}


@bp.get("/categories")
def categories():
    rows = db.session.query(Category.id, Category.name, func.count(Event.id)).join(Event).filter(Event.published.is_(True)).group_by(Category.id, Category.name).order_by(Category.name).all()
    return {"items": [{"id": row[0], "name": row[1], "count": row[2]} for row in rows]}


@bp.get("/stats")
def stats():
    return {"albums": Event.query.filter_by(published=True).count(),
            "photos": Photo.query.join(Event).filter(Event.published.is_(True)).count()}


@bp.get("/events/<slug>")
def event_detail(slug):
    event = Event.query.filter_by(slug=slug, published=True).first_or_404()
    return event_json(event, detail=True)


@bp.get("/photos/<int:photo_id>/content")
@limiter.limit("600 per minute")
def photo_content(photo_id):
    photo = Photo.query.join(Event).filter(Photo.id == photo_id, Event.published.is_(True)).first_or_404()
    return photo_response(photo)


@bp.get("/events/<slug>/download")
@limiter.limit("10 per minute")
def album_download(slug):
    event = Event.query.filter_by(slug=slug, published=True).first_or_404()
    selection = request.args.get("photos")
    photos = event.photos
    if selection is not None:
        try:
            ids = {int(value) for value in selection.split(",")}
        except ValueError:
            abort(400)
        if not ids or not ids.issubset({p.id for p in photos}):
            abort(400)
        photos = [p for p in photos if p.id in ids]
    return album_zip(event, photos)
