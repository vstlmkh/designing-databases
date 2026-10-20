from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0041_auto")]

    operations = [
        AddIndexConcurrently(
            model_name="event",
            index=models.Index(fields=["tenant_id", "created_at"], name="ev_tenant_created_idx"),
        ),
        migrations.RunPython(backfill_tenant),
    ]
