from __future__ import annotations

from pathlib import Path


def test_revision_template_is_repository_local_and_complete() -> None:
    template = Path("alembic/script.py.mako")
    assert template.is_file(), "alembic revision generation requires a repository-local template"
    content = template.read_text(encoding="utf-8")
    for marker in ("${up_revision}", "${down_revision", "def upgrade()", "def downgrade()"):
        assert marker in content


def test_initial_revision_materializes_deferred_cycle_foreign_keys() -> None:
    revision = Path("alembic/versions/0001_initial_schema.py").read_text(encoding="utf-8")
    for constraint_name in (
        "fk_documents_active_version",
        "fk_workflows_current_plan",
        "fk_workflows_pending_approval",
    ):
        assert f'op.create_foreign_key(\n        "{constraint_name}"' in revision
