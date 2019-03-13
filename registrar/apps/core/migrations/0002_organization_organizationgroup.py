# -*- coding: utf-8 -*-
# Generated by Django 1.11.18 on 2019-03-13 18:08
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0008_alter_user_username_max_length'),
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Organization',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('key', models.CharField(max_length=255, unique=True)),
                ('discovery_uuid', models.UUIDField(db_index=True, null=True)),
                ('name', models.CharField(max_length=255)),
            ],
            options={
                'permissions': (('organization_read_metadata', 'View Organization Metadata'), ('organization_read_enrollments', 'Read Organization enrollment data'), ('organization_write_enrollments', 'Read and Write Organization enrollment data')),
            },
        ),
        migrations.CreateModel(
            name='OrganizationGroup',
            fields=[
                ('group_ptr', models.OneToOneField(auto_created=True, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, serialize=False, to='auth.Group')),
                ('role', models.CharField(choices=[('organization_read_metadata', 'Read Metadata Only'), ('organization_read_enrollments', 'Read Enrollments Data'), ('organization_read_write_enrollments', 'Read and Write Enrollments Data')], default='organization_read_metadata', max_length=255)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core.Organization')),
            ],
            options={
                'verbose_name': 'Organization Group',
            },
            bases=('auth.group',),
        ),
    ]
