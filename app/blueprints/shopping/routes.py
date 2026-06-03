"""Routen für den Shopping-Blueprint."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import select

from app.blueprints.auth import user_has_any_role
from app.blueprints.shopping import bp
from app.domain.enums import Role
from app.extensions import db
from app.models.shopping import ShoppingItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _can_delete(item: ShoppingItem) -> bool:
    if not current_user.is_authenticated:
        return False
    if item.added_by_id is not None and item.added_by_id == current_user.id:
        return True
    return user_has_any_role(current_user, Role.HAUSWART, Role.ADMIN)


def _open_items() -> list[ShoppingItem]:
    stmt = (
        select(ShoppingItem)
        .where(ShoppingItem.bought_at.is_(None))
        .order_by(ShoppingItem.added_at.desc())
    )
    return list(db.session.scalars(stmt).all())


def _done_items() -> list[ShoppingItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    stmt = (
        select(ShoppingItem)
        .where(ShoppingItem.bought_at.is_not(None), ShoppingItem.bought_at >= cutoff)
        .order_by(ShoppingItem.bought_at.desc())
    )
    return list(db.session.scalars(stmt).all())


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _render_item_partial(item: ShoppingItem) -> str:
    can_delete = _can_delete(item)
    return render_template(
        "shopping/_item.html",
        item=item,
        can_delete=can_delete,
    )


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@bp.route("/", methods=["GET"])
@login_required
def index():
    open_items = _open_items()
    done_items = _done_items()
    # Pro Item entscheiden, ob der aktuelle User löschen darf.
    can_delete_map = {
        item.id: _can_delete(item) for item in (open_items + done_items)
    }
    return render_template(
        "shopping/index.html",
        open_items=open_items,
        done_items=done_items,
        can_delete_map=can_delete_map,
    )


@bp.route("/", methods=["POST"])
@login_required
def create():
    title = (request.form.get("title") or "").strip()
    quantity = (request.form.get("quantity") or "").strip() or None

    if not title:
        if _is_htmx():
            return ("", 400)
        flash("Titel ist erforderlich.", "error")
        return redirect(url_for("shopping.index"))

    item = ShoppingItem(
        title=title[:255],
        quantity=quantity[:100] if quantity else None,
        added_by_id=current_user.id,
    )
    db.session.add(item)
    db.session.commit()

    if _is_htmx():
        return _render_item_partial(item)

    flash("Eingekauft? Item hinzugefügt.", "success")
    return redirect(url_for("shopping.index"))


@bp.route("/<uuid:item_id>/check", methods=["POST"])
@login_required
def check(item_id: uuid.UUID):
    item = db.session.get(ShoppingItem, item_id)
    if item is None:
        abort(404)
    item.bought_at = datetime.now(timezone.utc)
    item.bought_by_id = current_user.id
    db.session.commit()

    if _is_htmx():
        return _render_item_partial(item)
    return redirect(url_for("shopping.index"))


@bp.route("/<uuid:item_id>/uncheck", methods=["POST"])
@login_required
def uncheck(item_id: uuid.UUID):
    item = db.session.get(ShoppingItem, item_id)
    if item is None:
        abort(404)
    item.bought_at = None
    item.bought_by_id = None
    db.session.commit()

    if _is_htmx():
        return _render_item_partial(item)
    return redirect(url_for("shopping.index"))


@bp.route("/<uuid:item_id>/delete", methods=["POST"])
@login_required
def delete(item_id: uuid.UUID):
    item = db.session.get(ShoppingItem, item_id)
    if item is None:
        abort(404)
    if not _can_delete(item):
        abort(403)

    db.session.delete(item)
    db.session.commit()

    if _is_htmx():
        # HTMX: leere Antwort, der Client soll das <li> entfernen (hx-swap=delete).
        return ("", 200)

    flash("Item gelöscht.", "info")
    return redirect(url_for("shopping.index"))
