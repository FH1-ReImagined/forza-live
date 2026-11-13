# Community videos (forza_live/content/videos.py) can be liked: the kind "video".

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('members', '0006_hub'),
    ]

    operations = [
        migrations.AlterField(
            model_name='like',
            name='kind',
            field=models.CharField(choices=[('photo', 'photo'), ('design', 'design'), ('video', 'video'), ('mod', 'mod'), ('pack', 'texture pack')], max_length=6),
        ),
        migrations.AlterField(
            model_name='missingitem',
            name='kind',
            field=models.CharField(choices=[('photo', 'photo'), ('design', 'design'), ('video', 'video'), ('mod', 'mod'), ('pack', 'texture pack')], max_length=6),
        ),
    ]
