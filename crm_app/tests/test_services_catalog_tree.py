"""
Tests for the catalog tree part of ProductService: sub-category (folder)
nesting, designs, image handling, and the cross-product / cross-company
ownership guards (app/services.py).

Image uploads are exercised with a tiny in-memory FileStorage so the
save/delete-on-disk paths are covered without any real image files.
"""

import io
import os

import pytest
from werkzeug.datastructures import FileStorage

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


@pytest.fixture
def product(container, seed):
    return container.product_service.create_product(
        seed.admin, product_name="Tiles", description="", hsn_code="6907",
        igst_percent="18", quantity="10", alternate_quantity="1.44")


def upload(filename="photo.png", data=b"fake-image-bytes"):
    return FileStorage(stream=io.BytesIO(data), filename=filename)


# ==========================================================================
# Folders (sub categories)
# ==========================================================================
class TestFolders:
    def test_create_folder_at_product_top_level(self, container, seed, product):
        f = container.product_service.create_folder(seed.admin, product.id, "Glossy", None)
        assert f.id is not None and f.product_id == product.id and f.parent_id is None

    def test_create_nested_folder(self, container, seed, product):
        parent = container.product_service.create_folder(seed.admin, product.id, "Glossy", None)
        child = container.product_service.create_folder(
            seed.admin, product.id, "600x600", parent.id)
        assert child.parent_id == parent.id

    def test_create_folder_requires_admin(self, container, seed, product):
        with pytest.raises(PermissionDeniedError):
            container.product_service.create_folder(seed.employee, product.id, "X", None)

    def test_create_folder_requires_name(self, container, seed, product):
        with pytest.raises(ValidationError):
            container.product_service.create_folder(seed.admin, product.id, "  ", None)

    def test_parent_from_another_product_rejected(self, container, seed, product):
        other_product = container.product_service.create_product(
            seed.admin, "Slabs", "", "", "", "", "")
        foreign_parent = container.product_service.create_folder(
            seed.admin, other_product.id, "Foreign", None)
        with pytest.raises(ValidationError):
            container.product_service.create_folder(
                seed.admin, product.id, "Child", foreign_parent.id)

    def test_rename_folder(self, container, seed, product):
        f = container.product_service.create_folder(seed.admin, product.id, "Old", None)
        container.product_service.rename_folder(seed.admin, f.id, "New")
        assert container.product_service.get_folder(f.id, seed.company_id).name == "New"

    def test_delete_folder_cascades_to_children(self, container, seed, product):
        parent = container.product_service.create_folder(seed.admin, product.id, "P", None)
        child = container.product_service.create_folder(seed.admin, product.id, "C", parent.id)
        container.product_service.delete_folder(seed.admin, parent.id)
        with pytest.raises(NotFoundError):
            container.product_service.get_folder(child.id, seed.company_id)

    def test_breadcrumb_is_empty_at_root(self, container, seed):
        assert container.product_service.breadcrumb(seed.company_id, None) == []

    def test_breadcrumb_walks_up_the_tree(self, container, seed, product):
        a = container.product_service.create_folder(seed.admin, product.id, "A", None)
        b = container.product_service.create_folder(seed.admin, product.id, "B", a.id)
        names = [f.name for f in container.product_service.breadcrumb(seed.company_id, b.id)]
        assert names[0] == "A" and "B" in names

    def test_folder_from_another_company_not_found(self, container, seed, product):
        f = container.product_service.create_folder(seed.admin, product.id, "Mine", None)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.product_service.get_folder(f.id, other.id)


# ==========================================================================
# list_contents
# ==========================================================================
class TestListContents:
    def test_top_level_lists_folders_and_designs(self, container, seed, product):
        container.product_service.create_folder(seed.admin, product.id, "Glossy", None)
        container.product_service.create_design(
            seed.admin, product.id, None, "White Marble", "", "12.5", "", None, None)
        folders, designs = container.product_service.list_contents(
            seed.company_id, product.id, None)
        assert [f.name for f in folders] == ["Glossy"]
        assert [d.design_name for d in designs] == ["White Marble"]

    def test_inside_a_folder(self, container, seed, product):
        folder = container.product_service.create_folder(seed.admin, product.id, "Glossy", None)
        container.product_service.create_design(
            seed.admin, product.id, folder.id, "Inside", "", "", "", None, None)
        folders, designs = container.product_service.list_contents(
            seed.company_id, product.id, folder.id)
        assert [d.design_name for d in designs] == ["Inside"]

    def test_folder_belonging_to_another_product_is_not_found(self, container, seed, product):
        other_product = container.product_service.create_product(
            seed.admin, "Slabs", "", "", "", "", "")
        foreign = container.product_service.create_folder(
            seed.admin, other_product.id, "Foreign", None)
        with pytest.raises(NotFoundError):
            container.product_service.list_contents(seed.company_id, product.id, foreign.id)


# ==========================================================================
# Designs
# ==========================================================================
class TestDesigns:
    def test_create_design_with_price_and_surface(self, container, seed, product):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "White Marble", "desc", "12.567",
            "alt text", None, None, surface="GLOSSY")
        assert d.id is not None
        assert d.price_usd == 12.57  # rounded to 2dp
        assert d.surface == "GLOSSY"

    def test_create_design_requires_admin(self, container, seed, product):
        with pytest.raises(PermissionDeniedError):
            container.product_service.create_design(
                seed.employee, product.id, None, "X", "", "", "", None, None)

    def test_create_design_requires_name(self, container, seed, product):
        with pytest.raises(ValidationError):
            container.product_service.create_design(
                seed.admin, product.id, None, "   ", "", "", "", None, None)

    def test_bad_price_rejected(self, container, seed, product):
        with pytest.raises(ValidationError):
            container.product_service.create_design(
                seed.admin, product.id, None, "D", "", "not-a-price", "", None, None)

    def test_design_in_folder_of_another_product_rejected(self, container, seed, product):
        other_product = container.product_service.create_product(
            seed.admin, "Slabs", "", "", "", "", "")
        foreign = container.product_service.create_folder(
            seed.admin, other_product.id, "Foreign", None)
        with pytest.raises(ValidationError):
            container.product_service.create_design(
                seed.admin, product.id, foreign.id, "D", "", "", "", None, None)

    def test_update_design(self, container, seed, product):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "Old Name", "", "10", "", None, None)
        container.product_service.update_design(
            seed.admin, d.id, "New Name", "new desc", "20", "alt", None, None, surface="MATT")
        reloaded = container.product_service.get_design(d.id, seed.company_id)
        assert reloaded.design_name == "New Name"
        assert reloaded.price_usd == 20.0
        assert reloaded.surface == "MATT"

    def test_delete_design(self, container, seed, product):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "Gone", "", "", "", None, None)
        container.product_service.delete_design(seed.admin, d.id)
        with pytest.raises(NotFoundError):
            container.product_service.get_design(d.id, seed.company_id)

    def test_list_designs_for_product(self, container, seed, product):
        container.product_service.create_design(
            seed.admin, product.id, None, "A", "", "", "", None, None)
        container.product_service.create_design(
            seed.admin, product.id, None, "B", "", "", "", None, None)
        designs = container.product_service.list_designs_for_product(product.id, seed.company_id)
        assert {d.design_name for d in designs} == {"A", "B"}

    def test_design_from_another_company_not_found(self, container, seed, product):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "Mine", "", "", "", None, None)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.product_service.get_design(d.id, other.id)


# ==========================================================================
# Image upload handling
# ==========================================================================
class TestDesignImages:
    def test_photo_saved_to_upload_folder(self, container, seed, product, tmp_config):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "With Photo", "", "", "",
            upload("tile.png"), None)
        assert d.photo_path.startswith("uploads/products/")
        on_disk = os.path.join(tmp_config.PRODUCT_UPLOAD_FOLDER,
                               os.path.basename(d.photo_path))
        assert os.path.exists(on_disk)

    def test_disallowed_extension_rejected(self, container, seed, product):
        with pytest.raises(ValidationError) as exc:
            container.product_service.create_design(
                seed.admin, product.id, None, "Bad", "", "", "",
                upload("payload.exe"), None)
        assert "Unsupported image type" in str(exc.value)

    def test_no_file_means_no_path(self, container, seed, product):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "No Photo", "", "", "", None, None)
        assert d.photo_path is None

    def test_deleting_design_removes_image_file(self, container, seed, product, tmp_config):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "Doomed", "", "", "", upload("x.png"), None)
        on_disk = os.path.join(tmp_config.PRODUCT_UPLOAD_FOLDER,
                               os.path.basename(d.photo_path))
        assert os.path.exists(on_disk)
        container.product_service.delete_design(seed.admin, d.id)
        assert not os.path.exists(on_disk)

    def test_replacing_photo_deletes_the_old_file(self, container, seed, product, tmp_config):
        d = container.product_service.create_design(
            seed.admin, product.id, None, "Swap", "", "", "", upload("old.png"), None)
        old_on_disk = os.path.join(tmp_config.PRODUCT_UPLOAD_FOLDER,
                                   os.path.basename(d.photo_path))
        container.product_service.update_design(
            seed.admin, d.id, "Swap", "", "", "", upload("new.png"), None)
        assert not os.path.exists(old_on_disk)
        reloaded = container.product_service.get_design(d.id, seed.company_id)
        assert os.path.exists(os.path.join(tmp_config.PRODUCT_UPLOAD_FOLDER,
                                           os.path.basename(reloaded.photo_path)))

    def test_uploaded_names_are_collision_proof(self, container, seed, product):
        a = container.product_service.create_design(
            seed.admin, product.id, None, "A", "", "", "", upload("same.png"), None)
        b = container.product_service.create_design(
            seed.admin, product.id, None, "B", "", "", "", upload("same.png"), None)
        assert a.photo_path != b.photo_path


# ==========================================================================
# Category deletion / tree
# ==========================================================================
class TestCategoryTree:
    def test_delete_category(self, container, seed):
        cat = container.product_service.create_category(seed.admin, "Doomed")
        container.product_service.delete_category(seed.admin, cat.id)
        with pytest.raises(NotFoundError):
            container.product_service.get_category(cat.id, seed.company_id)

    def test_delete_category_requires_admin(self, container, seed):
        cat = container.product_service.create_category(seed.admin, "Keep")
        with pytest.raises(PermissionDeniedError):
            container.product_service.delete_category(seed.employee, cat.id)

    def test_categories_tree_includes_nested(self, container, seed):
        parent = container.product_service.create_category(seed.admin, "Parent")
        container.product_service.create_category(seed.admin, "Child", parent_id=parent.id)
        tree = container.product_service.list_categories_tree(seed.company_id)
        names = [c.name for c, _depth in tree]
        assert "Parent" in names and "Child" in names

    def test_category_breadcrumb(self, container, seed):
        parent = container.product_service.create_category(seed.admin, "Parent")
        child = container.product_service.create_category(
            seed.admin, "Child", parent_id=parent.id)
        crumbs = container.product_service.category_breadcrumb(seed.company_id, child.id)
        assert [c.name for c in crumbs][0] == "Parent"

    def test_product_assigned_to_category(self, container, seed):
        cat = container.product_service.create_category(seed.admin, "Floor")
        p = container.product_service.create_product(
            seed.admin, "Tiles", "", "", "", "", "", category_id=cat.id)
        assert p.category_id == cat.id

    def test_foreign_category_id_rejected(self, container, seed):
        other = container.tenant_repo.create("Other", "other")
        other_admin = container.auth_service.create_user(
            other.id, "oadm", "pw123456", "O", "admin")
        foreign_cat = container.product_service.create_category(other_admin, "Foreign")
        with pytest.raises(NotFoundError):
            container.product_service.create_product(
                seed.admin, "Tiles", "", "", "", "", "", category_id=foreign_cat.id)
