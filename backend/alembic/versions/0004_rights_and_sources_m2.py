"""rights and sources (M2)

Creators and their channels, games, brand (output) channels, sources, versioned rights
records with evidence, source assignments and an append-only provenance log
(docs/M2_RIGHTS_SOURCES_PLAN.md §3, §12). Nothing is seeded (owner decision R5).

Immutability is enforced in the database (§12.6) with triggers:
* attested rights records: content columns can never change; ``superseded_by_id`` and
  ``revoked_at`` can each be set once; attested rows can never be deleted;
* coverage links of an attested record can be neither added nor removed;
* ``rights_evidence`` and ``provenance_events``: no UPDATE, no DELETE.
NOTE: a future ``batch_alter_table`` on these tables recreates them in SQLite and drops
their triggers; such a migration must recreate them (``_create_triggers``).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24 10:10:46.649767
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

_CONTENT = ("id, lineage_id, version, creator_id, basis, coverage, scope_json, scope_sha256, "
            "valid_from, valid_until, attestation_text, attested_by, attested_at, created_at")

TRIGGERS: dict[str, str] = {
    "trg_rights_attested_immutable": f"""
        CREATE TRIGGER trg_rights_attested_immutable
        BEFORE UPDATE OF {_CONTENT} ON rights_records
        WHEN OLD.attested_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'attested rights records are immutable'); END""",
    "trg_rights_superseded_once": """
        CREATE TRIGGER trg_rights_superseded_once
        BEFORE UPDATE OF superseded_by_id ON rights_records
        WHEN OLD.superseded_by_id IS NOT NULL
             OR OLD.attested_at IS NULL
             OR NEW.superseded_by_id IS NULL
        BEGIN SELECT RAISE(ABORT, 'supersession is set once, on attested records only'); END""",
    "trg_rights_revoked_once": """
        CREATE TRIGGER trg_rights_revoked_once
        BEFORE UPDATE OF revoked_at, revoked_reason ON rights_records
        WHEN OLD.revoked_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'revocation is permanent'); END""",
    "trg_rights_attested_no_delete": """
        CREATE TRIGGER trg_rights_attested_no_delete
        BEFORE DELETE ON rights_records
        WHEN OLD.attested_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'attested rights records cannot be deleted'); END""",
    "trg_rights_channels_frozen_ins": """
        CREATE TRIGGER trg_rights_channels_frozen_ins
        BEFORE INSERT ON rights_record_channels
        WHEN (SELECT attested_at FROM rights_records WHERE id = NEW.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_channels_frozen_del": """
        CREATE TRIGGER trg_rights_channels_frozen_del
        BEFORE DELETE ON rights_record_channels
        WHEN (SELECT attested_at FROM rights_records WHERE id = OLD.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_sources_frozen_ins": """
        CREATE TRIGGER trg_rights_sources_frozen_ins
        BEFORE INSERT ON rights_source_links
        WHEN (SELECT attested_at FROM rights_records WHERE id = NEW.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_sources_frozen_del": """
        CREATE TRIGGER trg_rights_sources_frozen_del
        BEFORE DELETE ON rights_source_links
        WHEN (SELECT attested_at FROM rights_records WHERE id = OLD.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_evidence_no_update": """
        CREATE TRIGGER trg_evidence_no_update BEFORE UPDATE ON rights_evidence
        BEGIN SELECT RAISE(ABORT, 'rights evidence is append-only'); END""",
    "trg_evidence_no_delete": """
        CREATE TRIGGER trg_evidence_no_delete BEFORE DELETE ON rights_evidence
        BEGIN SELECT RAISE(ABORT, 'rights evidence is append-only'); END""",
    "trg_provenance_no_update": """
        CREATE TRIGGER trg_provenance_no_update BEFORE UPDATE ON provenance_events
        BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END""",
    "trg_provenance_no_delete": """
        CREATE TRIGGER trg_provenance_no_delete BEFORE DELETE ON provenance_events
        BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END""",
}


def _create_triggers() -> None:
    for sql in TRIGGERS.values():
        op.execute(sql)


def upgrade() -> None:
    op.create_table('brand_channels',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('handle', sa.String(length=128), nullable=True),
    sa.Column('monetized', sa.Boolean(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("platform IN ('youtube_shorts','instagram_reels','facebook_reels','x')", name=op.f('ck_brand_channels_platform_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_brand_channels')),
    sa.UniqueConstraint('slug', name=op.f('uq_brand_channels_slug'))
    )
    op.create_table('creators',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('display_name', sa.String(length=128), nullable=False),
    sa.Column('relationship', sa.String(length=16), nullable=False),
    sa.Column('contact', sa.String(length=256), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("relationship IN ('self','partner','licensor')", name=op.f('ck_creators_relationship_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_creators'))
    )
    op.create_table('games',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_games')),
    sa.UniqueConstraint('slug', name=op.f('uq_games_slug'))
    )
    op.create_table('provenance_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entity', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=16), nullable=False),
    sa.Column('details_json', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("actor IN ('owner','system')", name=op.f('ck_provenance_events_actor_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provenance_events'))
    )
    with op.batch_alter_table('provenance_events', schema=None) as batch_op:
        batch_op.create_index('ix_provenance_entity', ['entity', 'entity_id'], unique=False)

    op.create_table('creator_channels',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('creator_id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('platform_channel_id', sa.String(length=128), nullable=True),
    sa.Column('handle', sa.String(length=128), nullable=True),
    sa.Column('url_canonical', sa.String(length=512), nullable=True),
    sa.Column('verified_by_owner_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("platform IN ('youtube','twitch','local')", name=op.f('ck_creator_channels_platform_valid')),
    sa.ForeignKeyConstraint(['creator_id'], ['creators.id'], name=op.f('fk_creator_channels_creator_id_creators')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_creator_channels')),
    sa.UniqueConstraint('platform', 'handle', name='uq_channel_platform_handle'),
    sa.UniqueConstraint('platform', 'platform_channel_id', name='uq_channel_platform_id')
    )
    op.create_table('rights_records',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('lineage_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('creator_id', sa.String(length=36), nullable=False),
    sa.Column('basis', sa.String(length=32), nullable=False),
    sa.Column('coverage', sa.String(length=16), nullable=False),
    sa.Column('scope_json', sa.Text(), nullable=False),
    sa.Column('scope_sha256', sa.String(length=64), nullable=False),
    sa.Column('valid_from', sa.DateTime(), nullable=False),
    sa.Column('valid_until', sa.DateTime(), nullable=True),
    sa.Column('attestation_text', sa.Text(), nullable=True),
    sa.Column('attested_by', sa.String(length=64), nullable=True),
    sa.Column('attested_at', sa.DateTime(), nullable=True),
    sa.Column('superseded_by_id', sa.String(length=36), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("basis IN ('ownership','creator_permission','licence')", name=op.f('ck_rights_records_basis_valid')),
    sa.CheckConstraint("coverage IN ('creator','channels','sources')", name=op.f('ck_rights_records_coverage_valid')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_rights_records_version_positive')),
    sa.ForeignKeyConstraint(['creator_id'], ['creators.id'], name=op.f('fk_rights_records_creator_id_creators')),
    sa.ForeignKeyConstraint(['superseded_by_id'], ['rights_records.id'], name=op.f('fk_rights_records_superseded_by_id_rights_records')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rights_records')),
    sa.UniqueConstraint('lineage_id', 'version', name='uq_rights_lineage_version')
    )
    with op.batch_alter_table('rights_records', schema=None) as batch_op:
        batch_op.create_index('ix_rights_creator', ['creator_id'], unique=False)

    op.create_table('rights_evidence',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('rights_record_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('reference', sa.Text(), nullable=True),
    sa.Column('original_filename', sa.String(length=255), nullable=True),
    sa.Column('stored_name', sa.String(length=64), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("kind IN ('email','document','platform_permission','note')", name=op.f('ck_rights_evidence_kind_valid')),
    sa.CheckConstraint('size_bytes IS NULL OR size_bytes >= 0', name=op.f('ck_rights_evidence_size_nonneg')),
    sa.ForeignKeyConstraint(['rights_record_id'], ['rights_records.id'], name=op.f('fk_rights_evidence_rights_record_id_rights_records')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rights_evidence'))
    )
    op.create_table('rights_record_channels',
    sa.Column('rights_record_id', sa.String(length=36), nullable=False),
    sa.Column('creator_channel_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['creator_channel_id'], ['creator_channels.id'], name=op.f('fk_rights_record_channels_creator_channel_id_creator_channels')),
    sa.ForeignKeyConstraint(['rights_record_id'], ['rights_records.id'], name=op.f('fk_rights_record_channels_rights_record_id_rights_records')),
    sa.PrimaryKeyConstraint('rights_record_id', 'creator_channel_id', name=op.f('pk_rights_record_channels'))
    )
    op.create_table('sources',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('platform_video_id', sa.String(length=128), nullable=True),
    sa.Column('url_canonical', sa.String(length=512), nullable=True),
    sa.Column('start_hint_ms', sa.BigInteger(), nullable=True),
    sa.Column('creator_channel_id', sa.String(length=36), nullable=True),
    sa.Column('title_user', sa.String(length=255), nullable=True),
    sa.Column('game_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('acquisition_method', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("acquisition_method IN ('local_recording','creator_file','platform_export','direct_link','platform_api','watch_folder','url_fetch')", name=op.f('ck_sources_acquisition_method_valid')),
    sa.CheckConstraint("kind IN ('local','youtube','twitch')", name=op.f('ck_sources_kind_valid')),
    sa.CheckConstraint("status IN ('active','blocked','archived')", name=op.f('ck_sources_status_valid')),
    sa.ForeignKeyConstraint(['creator_channel_id'], ['creator_channels.id'], name=op.f('fk_sources_creator_channel_id_creator_channels')),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], name=op.f('fk_sources_game_id_games')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sources')),
    sa.UniqueConstraint('url_canonical', name='uq_sources_url')
    )
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.create_index('ix_sources_channel', ['creator_channel_id'], unique=False)

    op.create_table('rights_source_links',
    sa.Column('rights_record_id', sa.String(length=36), nullable=False),
    sa.Column('source_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['rights_record_id'], ['rights_records.id'], name=op.f('fk_rights_source_links_rights_record_id_rights_records')),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_rights_source_links_source_id_sources')),
    sa.PrimaryKeyConstraint('rights_record_id', 'source_id', name=op.f('pk_rights_source_links'))
    )
    op.create_table('source_assignments',
    sa.Column('source_id', sa.String(length=36), nullable=False),
    sa.Column('brand_channel_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['brand_channel_id'], ['brand_channels.id'], name=op.f('fk_source_assignments_brand_channel_id_brand_channels')),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_source_assignments_source_id_sources')),
    sa.PrimaryKeyConstraint('source_id', 'brand_channel_id', name=op.f('pk_source_assignments'))
    )
    with op.batch_alter_table('recordings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('source_offset_ms', sa.BigInteger(), nullable=False,
                                      server_default='0'))
        batch_op.create_foreign_key(batch_op.f('fk_recordings_source_id_sources'), 'sources', ['source_id'], ['id'])

    _create_triggers()


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    with op.batch_alter_table('recordings', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_recordings_source_id_sources'), type_='foreignkey')
        batch_op.drop_column('source_offset_ms')
        batch_op.drop_column('source_id')

    op.drop_table('source_assignments')
    op.drop_table('rights_source_links')
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_index('ix_sources_channel')

    op.drop_table('sources')
    op.drop_table('rights_record_channels')
    op.drop_table('rights_evidence')
    with op.batch_alter_table('rights_records', schema=None) as batch_op:
        batch_op.drop_index('ix_rights_creator')

    op.drop_table('rights_records')
    op.drop_table('creator_channels')
    with op.batch_alter_table('provenance_events', schema=None) as batch_op:
        batch_op.drop_index('ix_provenance_entity')

    op.drop_table('provenance_events')
    op.drop_table('games')
    op.drop_table('creators')
    op.drop_table('brand_channels')
