from django.db import migrations


def clear_deleted_at(apps, schema_editor):
    """
    Restore soft-deleted rows for reporting. Partial unique (owner, name) where deleted_at
    is null requires renaming when an active row already uses the same name.
    """
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, owner_id, name
            FROM journal_tradingaccount
            WHERE deleted_at IS NOT NULL
            ORDER BY id
            """
        )
        soft_rows = cursor.fetchall()

    def alive_name_taken(cursor, owner_id, name):
        cursor.execute(
            """
            SELECT 1 FROM journal_tradingaccount
            WHERE owner_id = %s AND name = %s AND deleted_at IS NULL
            LIMIT 1
            """,
            [owner_id, name],
        )
        return cursor.fetchone() is not None

    with conn.cursor() as cursor:
        for pk, owner_id, name in soft_rows:
            new_name = name
            if alive_name_taken(cursor, owner_id, new_name):
                new_name = f"{name} (restored {pk})"
                while alive_name_taken(cursor, owner_id, new_name):
                    new_name = f"{new_name}_{pk}"
            cursor.execute(
                """
                UPDATE journal_tradingaccount
                SET name = %s, deleted_at = NULL
                WHERE id = %s
                """,
                [new_name, pk],
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("journal", "0011_trading_account_soft_tags_user_import"),
    ]

    operations = [
        migrations.RunPython(clear_deleted_at, noop_reverse),
    ]
