"""
app/routes/inventory.py
------------------------
Inventory: the same catalog browser as Products (categories -> products ->
sub categories -> designs), but read-only and focused on stock on hand. The
design tables gain a "current stock" column, and a design's page drops the
Product tax/packing block in favour of an Inventory block plus a
Purchase/Sale history of every time it was received or sold.

Stock per design is everything RECEIVED (listed on a purchase invoice's own
packing list), less everything DISPATCHED back out for job work (belonging to
an invoice that has a Job Out challan against it), less everything SOLD
(allocated on an export invoice's Designs Packing List). Goods coming back
from job work aren't modelled yet - that's the Job In document, still to be
built. See InventoryService for the arithmetic.

Everyone signed in can browse; nothing here is editable (stock is derived
from the documents themselves, so it's maintained through the Documents
pipeline, not typed in here).
"""

from flask import Blueprint, render_template, current_app, g, abort, request

from app.exceptions import NotFoundError
from app.utils import login_required

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _int_or_none(value):
    return int(value) if value not in (None, "", "None") else None


# ============================================================
# CATALOG ROOT (categories as folders + uncategorised products)
# ============================================================
@inventory_bp.route("/")
@inventory_bp.route("/category/<int:category_id>")
@login_required
def list_inventory(category_id=None):
    container = current_app.container
    products_service = container.product_service
    try:
        subcategories, products = products_service.list_catalog(g.user.company_id, category_id)
        current_category = products_service.get_category(category_id, g.user.company_id) if category_id else None
    except NotFoundError:
        abort(404)
    breadcrumb = products_service.category_breadcrumb(g.user.company_id, category_id)
    in_stock = container.inventory_service.in_stock_designs(g.user.company_id) if category_id is None else []
    query = request.args.get("q", "")
    search_results = products_service.search_catalog(g.user.company_id, query)
    if search_results["designs"]:
        stock_by_design = container.inventory_service.stock_by_design(g.user.company_id)
        for design in search_results["designs"]:
            design["stock"] = stock_by_design.get(
                design["id"],
                {"boxes": 0, "pcs": 0, "quantity": 0,
                 "dispatched_boxes": 0, "dispatched_quantity": 0,
                 "sold_boxes": 0, "sold_quantity": 0,
                 "net_boxes": 0, "net_pcs": 0, "net_quantity": 0,
                 "unit": None, "qty_unit": None},
            )
    return render_template("inventory/list.html", categories=subcategories, products=products,
                           current_category=current_category, breadcrumb=breadcrumb, in_stock=in_stock,
                           query=query, search_results=search_results)


# ============================================================
# PRODUCT (doubles as the sub-category browser)
# ============================================================
@inventory_bp.route("/product/<int:product_id>")
@inventory_bp.route("/product/<int:product_id>/folder/<int:folder_id>")
@login_required
def view_product(product_id, folder_id=None):
    container = current_app.container
    products_service = container.product_service
    try:
        product = products_service.get_product(product_id, g.user.company_id)
        subfolders, designs = products_service.list_contents(g.user.company_id, product_id, folder_id)
        current_folder = products_service.get_folder(folder_id, g.user.company_id) if folder_id else None
        category = products_service.get_category(product.category_id, g.user.company_id) \
            if product.category_id else None
    except NotFoundError:
        abort(404)
    breadcrumb = products_service.breadcrumb(g.user.company_id, folder_id)
    stock_by_design = container.inventory_service.stock_by_design(g.user.company_id)
    return render_template(
        "inventory/detail.html", product=product, category=category, current_folder=current_folder,
        breadcrumb=breadcrumb, subfolders=subfolders, designs=designs, stock_by_design=stock_by_design,
    )


# ============================================================
# DESIGN (stock on hand + purchase/sale history)
# ============================================================
@inventory_bp.route("/design/<int:design_id>")
@login_required
def view_design(design_id):
    container = current_app.container
    products_service = container.product_service
    try:
        design = products_service.get_design(design_id, g.user.company_id)
        product = products_service.get_product(design.product_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    breadcrumb = products_service.breadcrumb(g.user.company_id, design.folder_id)
    stock = container.inventory_service.stock_for_design(g.user.company_id, design_id)
    history = container.inventory_service.purchase_sale_history(g.user.company_id, design_id)
    stock_history = container.inventory_service.stock_history_summary(g.user.company_id, design_id)
    return render_template("inventory/design_detail.html", design=design, product=product,
                           breadcrumb=breadcrumb, stock=stock, history=history, stock_history=stock_history)
