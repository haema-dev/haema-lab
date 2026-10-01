"""Apply qa/sql/schema.sql, the source of truth for the qa tables.

The models are managed=False, so 0001 creates nothing; this runs the SQL file
(idempotent: CREATE ... IF NOT EXISTS). PostgreSQL only: on sqlite (core/
tests without a DB server) it does nothing.
"""

from pathlib import Path

from django.db import migrations

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def apply_schema(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # The migration already runs in a transaction; drop the file's own BEGIN/COMMIT.
    sql = "\n".join(
        line for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        if line.strip() not in {"BEGIN;", "COMMIT;"}
    )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [("qa", "0001_initial")]

    operations = [migrations.RunPython(apply_schema, migrations.RunPython.noop)]
