"""Extras-Blueprint: freiwillige Sonderleistungen + Ehrenpunkte.

* **Bewohner** reichen unter ``/extras`` eine Sonderleistung mit Beschreibung
  ein und sehen ihre eigene Historie.
* **Hauswart/Admin** sehen auf derselben Seite oben einen Prüf-Bereich und
  vergeben beim Genehmigen Ehrenpunkte (→ HONOR-KarmaEvent, hebt den Score).

Die State-Changes delegieren komplett an ``app.services.contributions`` — hier
wird nichts reimplementiert. Genehmigen/Ablehnen sind HTMX-Endpunkte und
rendern die Zeile als Partial (``_review_row.html``).
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.auth import require_admin_or_hauswart, user_has_any_role
from app.domain.enums import Role
from app.extensions import db
from app.models.extra import ExtraContribution
from app.services.contributions import (
    approve_contribution,
    pending_contributions,
    reject_contribution,
    submit_contribution,
    user_contributions,
)

bp = Blueprint("extras", __name__, template_folder="../../templates/extras")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _get_contribution_or_404(contribution_id) -> ExtraContribution:
    contribution = db.session.get(ExtraContribution, contribution_id)
    if contribution is None:
        abort(404)
    return contribution


def _row_response(contribution: ExtraContribution):
    """HTMX → Zeile als Partial; sonst zurück auf die Übersicht."""
    if _is_htmx():
        return render_template("extras/_review_row.html", contribution=contribution)
    return redirect(url_for("extras.index"))


@bp.route("/")
@login_required
def index():
    can_review = user_has_any_role(current_user, Role.HAUSWART, Role.ADMIN)
    pending = pending_contributions(db.session) if can_review else []
    mine = user_contributions(db.session, current_user)
    return render_template(
        "extras/index.html",
        pending=pending,
        mine=mine,
        can_review=can_review,
    )


@bp.route("/", methods=["POST"])
@login_required
def create():
    description = (request.form.get("description") or "").strip()
    if not description:
        flash("Bitte beschreibe kurz, was du gemacht hast.", "warning")
        return redirect(url_for("extras.index"))
    submit_contribution(db.session, current_user, description)
    db.session.commit()
    flash("Sonderleistung eingereicht – der Hauswart prüft sie.", "success")
    return redirect(url_for("extras.index"))


@bp.route("/<uuid:contribution_id>/approve", methods=["POST"])
@login_required
def approve(contribution_id):
    require_admin_or_hauswart()
    contribution = _get_contribution_or_404(contribution_id)

    raw = (request.form.get("honor_points") or "").strip()
    try:
        points = int(raw)
    except ValueError:
        points = 0
    if points < 1:
        flash("Bitte eine gültige Punktzahl (≥ 1) angeben.", "warning")
        if _is_htmx():
            return render_template("extras/_review_row.html", contribution=contribution)
        return redirect(url_for("extras.index"))

    note = (request.form.get("note") or "").strip() or None
    approve_contribution(db.session, contribution, current_user, points, note=note)
    db.session.commit()
    return _row_response(contribution)


@bp.route("/<uuid:contribution_id>/reject", methods=["POST"])
@login_required
def reject(contribution_id):
    require_admin_or_hauswart()
    contribution = _get_contribution_or_404(contribution_id)
    note = (request.form.get("note") or "").strip() or None
    reject_contribution(db.session, contribution, current_user, note=note)
    db.session.commit()
    return _row_response(contribution)
