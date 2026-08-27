"""
app/routes/booking_details.py
------------------------------
"Booking Detail" - a standalone shipping-booking log under Master Data, with
the same field shape as an Export Invoice's own "Container details" card
(booking no. / vessel / voyage, one transporter for the whole booking, the
container type/count list, and one row per physical container) - but owned
directly by a Buyer rather than any invoice, so a booking can be logged on
its own. Reads are open to anyone signed in; writes are admin-only, the same
split every other master-data directory entity (Transporter, Buyer) uses.
"""

import zipfile

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort, jsonify

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required, verify_delete_password

booking_details_bp = Blueprint("booking_details", __name__, url_prefix="/booking-details")

_FIELDS = ["buyer_id", "booking_no", "vessel_name", "voyage_no", "transporter_name"]

# Container details (11B) Excel upload: header row is matched against these
# field names (case/punctuation/whitespace-insensitive - see _normalize_header)
# rather than a fixed column order, so the sheet just needs the same field
# names the table itself uses, in any order, with any extra columns ignored.
_EXCEL_HEADER_MAP = {
    "container no": "container_no",
    "max permitted weight": "max_permitted_weight",
    "tare weight": "tare_weight_kg",
    "vehicle no": "vehicle_no",
    "lr no": "lr_no",
    "line seal no": "line_seal_no",
    "rfid seal no": "rfid_seal_no",
}


def _normalize_header(value) -> str:
    text = str(value or "").strip().lower().replace("_", " ").replace(".", "")
    return " ".join(text.split())


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _extract_fields(form) -> dict:
    return {key: form.get(key, "") for key in _FIELDS}


def _extract_containers(form) -> list:
    types = form.getlist("container_type[]")
    counts = form.getlist("container_count[]")
    rows = []
    for i in range(max(len(types), len(counts))):
        rows.append({
            "container_type": types[i] if i < len(types) else "",
            "container_count": counts[i] if i < len(counts) else "",
        })
    return rows


def _extract_container_details(form) -> list:
    types = form.getlist("cd_container_type[]")
    nos = form.getlist("cd_container_no[]")
    max_weights = form.getlist("cd_max_permitted_weight[]")
    tares = form.getlist("cd_tare_weight_kg[]")
    vehicles = form.getlist("cd_vehicle_no[]")
    lr_nos = form.getlist("cd_lr_no[]")
    line_seals = form.getlist("cd_line_seal_no[]")
    rfids = form.getlist("cd_rfid_seal_no[]")
    n = max(len(types), len(nos), len(max_weights), len(tares), len(vehicles),
            len(lr_nos), len(line_seals), len(rfids))
    rows = []
    for i in range(n):
        rows.append({
            "container_type": types[i] if i < len(types) else "",
            "container_no": nos[i] if i < len(nos) else "",
            "max_permitted_weight": max_weights[i] if i < len(max_weights) else "",
            "tare_weight_kg": tares[i] if i < len(tares) else "",
            "vehicle_no": vehicles[i] if i < len(vehicles) else "",
            "lr_no": lr_nos[i] if i < len(lr_nos) else "",
            "line_seal_no": line_seals[i] if i < len(line_seals) else "",
            "rfid_seal_no": rfids[i] if i < len(rfids) else "",
        })
    return rows


def _form_context(container, company_id):
    return {
        "buyers": container.buyer_repo.list_all(company_id),
        # Administration -> Miscellaneous -> Container type (an admin-managed
        # list, same treatment as currency/nature-of-contract/port-of-loading).
        # Falls back to the list this used to be hard-coded to until a
        # company adds its own rows, so nothing goes empty on upgrade.
        "container_types": [ct.name for ct in container.misc_list_service.container_type_options(company_id)],
        "transporters": container.transporter_service.list_all(company_id),
    }


@booking_details_bp.route("/api/parse-excel", methods=["POST"])
@admin_required
def parse_container_details_excel():
    """Reads an uploaded .xlsx's first sheet: the header row's column names
    are matched against the Container details (11B) table's own field names
    (Container no., Max permitted weight, Tare weight, Vehicle no., LR no.,
    Line seal no., RFID seal no.), then every following non-blank row becomes
    one 11B row - lets a booking's containers be typed in Excel and pasted in
    wholesale instead of one row at a time. Columns that don't match a known
    field name are ignored; unmatched fields on a matched row are blank."""
    file = request.files.get("excel_file")
    if not file or not file.filename:
        return jsonify({"error": "Choose an Excel file first."}), 400
    if not file.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Only .xlsx files are supported."}), 400

    try:
        workbook = openpyxl.load_workbook(file.stream, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError):
        return jsonify({"error": "Couldn't read that file - is it a valid .xlsx?"}), 400

    sheet = workbook.worksheets[0]
    rows_iter = sheet.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        return jsonify({"error": "The file is empty."}), 400

    column_map = {}  # column index -> field key
    for i, cell in enumerate(header_row):
        field = _EXCEL_HEADER_MAP.get(_normalize_header(cell))
        if field:
            column_map[i] = field

    if not column_map:
        return jsonify({"error": "None of the columns match a Container details field "
                                  "(Container no., Max permitted weight, Tare weight, "
                                  "Vehicle no., LR no., Line seal no., RFID seal no.)."}), 400

    rows = []
    for raw_row in rows_iter:
        if not raw_row or all(c in (None, "") for c in raw_row):
            continue
        row = {field: "" for field in _EXCEL_HEADER_MAP.values()}
        for i, field in column_map.items():
            row[field] = _cell_text(raw_row[i]) if i < len(raw_row) else ""
        if any(row.values()):
            rows.append(row)
        if len(rows) >= 500:
            break

    return jsonify({"rows": rows})


@booking_details_bp.route("/api/<int:booking_detail_id>/containers")
@login_required
def booking_containers(booking_detail_id):
    """This booking's container rows, for whoever wants to fill their own
    Container details table from it - the Export Invoice's section 11B and
    Loading Planning both do.

    The caller COPIES what comes back rather than linking to it: the two
    tables already share every meaningful column, and a snapshot is what
    stops a later edit of the booking from rewriting an already-issued
    invoice. Read-only, so login is enough - unlike the write routes here."""
    try:
        return jsonify(
            current_app.container.loading_planning_service.booking_snapshot(
                booking_detail_id, g.user.company_id
            )
        )
    except NotFoundError:
        return jsonify({"error": "Booking not found."}), 404


@booking_details_bp.route("/")
@login_required
def list_booking_details():
    bookings = current_app.container.booking_detail_service.list_all(g.user.company_id)
    return render_template("booking_details/list.html", bookings=bookings)


@booking_details_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_booking_detail():
    container = current_app.container
    if request.method == "POST":
        try:
            booking = container.booking_detail_service.create(
                current_user=g.user, fields=_extract_fields(request.form),
                containers=_extract_containers(request.form),
                container_details=_extract_container_details(request.form),
            )
            flash(f"Booking detail {booking.booking_no or ('#' + str(booking.id))} added.", "success")
            return redirect(url_for("booking_details.list_booking_details"))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template(
                "booking_details/form.html", booking=None, form_data=request.form,
                form_containers=_extract_containers(request.form),
                form_container_details=_extract_container_details(request.form),
                **_form_context(container, g.user.company_id),
            ), 400

    return render_template(
        "booking_details/form.html", booking=None, form_data=None,
        form_containers=None, form_container_details=None,
        **_form_context(container, g.user.company_id),
    )


@booking_details_bp.route("/<int:booking_detail_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_booking_detail(booking_detail_id):
    container = current_app.container
    try:
        booking = container.booking_detail_service.get(booking_detail_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            updated = container.booking_detail_service.update(
                booking_detail_id=booking_detail_id, current_user=g.user,
                fields=_extract_fields(request.form),
                containers=_extract_containers(request.form),
                container_details=_extract_container_details(request.form),
            )
            flash(f"Booking detail {updated.booking_no or ('#' + str(updated.id))} updated.", "success")
            return redirect(url_for("booking_details.list_booking_details"))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template(
                "booking_details/form.html", booking=booking, form_data=request.form,
                form_containers=_extract_containers(request.form),
                form_container_details=_extract_container_details(request.form),
                **_form_context(container, g.user.company_id),
            ), 400

    return render_template(
        "booking_details/form.html", booking=booking, form_data=None,
        form_containers=None, form_container_details=None,
        **_form_context(container, g.user.company_id),
    )


@booking_details_bp.route("/<int:booking_detail_id>/delete", methods=["POST"])
@admin_required
def delete_booking_detail(booking_detail_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Booking detail not deleted.", "error")
        return redirect(url_for("booking_details.list_booking_details"))
    try:
        booking = current_app.container.booking_detail_service.get(booking_detail_id, g.user.company_id)
        current_app.container.booking_detail_service.delete(booking_detail_id, g.user)
        flash(f"Booking detail {booking.booking_no or ('#' + str(booking.id))} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("booking_details.list_booking_details"))
