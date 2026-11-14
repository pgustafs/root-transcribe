"""Standalone boundaries: routes, task registration and fresh database schema."""

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.db.base import Base
from app.main import app
from app.worker import tasks  # noqa: F401
from app.worker.celery_app import celery_app


def test_only_transcription_support_routes_are_exposed():
    paths = set(app.openapi()["paths"])
    assert "/api/v1/workspaces/{workspace_id}/recordings" in paths
    assert "/api/v1/speakers" in paths
    # Meeting notes written by agents are part of this app (added deliberately).
    assert "/api/v1/workspaces/{workspace_id}/recordings/{recording_id}/notes" in paths
    assert "/api/v1/note-agents" in paths
    forbidden = (
        "/chat",
        "/rag",
        "/podcasts",
        "/recap",
        "/ask",
        "/save-as-source",
        "/collections",
        "/integrations",
        "/groups",
    )
    assert not any(fragment in path for fragment in forbidden for path in paths)


def test_openapi_operations_have_unique_ids():
    operations = [
        operation["operationId"]
        for path in app.openapi()["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operations) == len(set(operations))


def test_all_scheduled_and_routed_tasks_exist():
    names = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    names.update(celery_app.conf.task_routes)
    assert names <= set(celery_app.tasks)
    assert (
        celery_app.conf.task_routes["tasks.transcribe_attempt"]["queue"]
        == "transcription"
    )
    assert not any("podcast" in name or "ingest" in name for name in celery_app.tasks)


def test_fresh_migration_matches_retained_models_and_can_downgrade():
    scripts = ScriptDirectory(str(Path(__file__).parents[2] / "app/alembic"))
    migrations = list(reversed(list(scripts.walk_revisions())))
    with create_engine("sqlite://").begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for migration in migrations:
                migration.module.upgrade()
            assert compare_metadata(context, Base.metadata) == []
            for migration in reversed(migrations):
                migration.module.downgrade()
        assert not connection.dialect.get_table_names(connection)


def test_recording_language_migration_preserves_detected_language():
    scripts = ScriptDirectory(str(Path(__file__).parents[2] / "app/alembic"))
    migration = scripts.get_revision("0002_recording_language").module
    with create_engine("sqlite://").begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE meeting_recording (id INTEGER PRIMARY KEY, language TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO meeting_recording (id, language) VALUES (1, 'sv')"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.exec_driver_sql(
                "SELECT language, requested_language FROM meeting_recording"
            ).one() == ("sv", "auto")
            migration.downgrade()
            assert (
                connection.exec_driver_sql(
                    "SELECT language FROM meeting_recording"
                ).scalar()
                == "sv"
            )
