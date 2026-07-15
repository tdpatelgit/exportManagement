"""
app/routes/backup.py
---------------------
Admin-only "Database Backup" section.

Three things an admin can do here:
  * download a full snapshot of everything (SQLite DB + product images) as one
    ZIP they can keep off-server;
  * restore a snapshot they previously downloaded (with a signature/format
    check so uploading the wrong file can't silently trash the data);
  * download the automatic pre-migration snapshots the app keeps in
    instance/backups/.

All business logic lives in BackupService - these views only turn its results
(or the ValidationError it raises) into an HTTP response, exactly like the
other route modules. Every view is @admin_required: the sidebar link is only a
convenience, the real gate is here.
"""

import io
import os

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    send_file, current_app,
)

from app.utils import admin_required
from app.exceptions import ValidationError, NotFoundError

backup_bp = Blueprint("backup", __name__, url_prefix="/backup")


@backup_bp.route("/")
@admin_required
def index():
    service = current_app.container.backup_service
    return render_template("backup/index.html", auto_backups=service.list_auto_backups())


@backup_bp.route("/download")
@admin_required
def download():
    service = current_app.container.backup_service
    zip_path, download_name = service.create_backup_zip()
    # Read into memory and delete the temp file straight away, so cleanup never
    # races the response stream.
    try:
        with open(zip_path, "rb") as f:
            data = io.BytesIO(f.read())
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    data.seek(0)
    return send_file(
        data, as_attachment=True, download_name=download_name, mimetype="application/zip",
    )


@backup_bp.route("/restore", methods=["POST"])
@admin_required
def restore():
    if not request.form.get("confirm"):
        flash("Please tick the confirmation box - restoring overwrites all current data.", "warning")
        return redirect(url_for("backup.index"))

    service = current_app.container.backup_service
    try:
        result = service.restore_from_zip(request.files.get("backup_file"))
    except (ValidationError, NotFoundError) as e:
        flash(str(e), "error")
        return redirect(url_for("backup.index"))

    made_on = result.get("created_at") or "an earlier snapshot"
    flash(
        f"Database restored from backup taken {made_on}. A snapshot of your previous "
        "data was saved automatically under Automatic snapshots below.",
        "success",
    )
    return redirect(url_for("backup.index"))


@backup_bp.route("/auto/<name>/download")
@admin_required
def download_auto(name):
    service = current_app.container.backup_service
    try:
        path = service.get_auto_backup_path(name)
    except (ValidationError, NotFoundError) as e:
        flash(str(e), "error")
        return redirect(url_for("backup.index"))
    return send_file(
        path, as_attachment=True, download_name=os.path.basename(path),
        mimetype="application/octet-stream",
    )
