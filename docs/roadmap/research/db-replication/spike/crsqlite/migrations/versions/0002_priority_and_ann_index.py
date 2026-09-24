"""priority column and ANN index

- tasks.priority: ALTER on a CRR must be wrapped in crsql_begin_alter() /
  crsql_commit_alter(). Alembic's batch mode emits a plain ALTER TABLE ADD
  COLUMN for this case (no table copy).
- ANN index: sqlite-vec vec0 virtual table with a DiskANN index (sqlite-vec
  0.1.10 alpha). A virtual table cannot be a CRR, so vec_embeddings is a local
  index over the replicated embeddings.vec BLOBs, keyed by the local rowid of
  embeddings, maintained by triggers that also fire when cr-sqlite merges
  remote changes, and backfilled here from existing rows.

Revision ID: 0002_priority_and_ann_index
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_priority_and_ann_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

VEC_INDEX = "diskann(neighbor_quantizer=binary)"
VEC_OK = "length(new.vec) = 1536"


def upgrade() -> None:
    op.execute("SELECT crsql_begin_alter('tasks')")
    with op.batch_alter_table("tasks") as b:
        b.add_column(sa.Column("priority", sa.Integer, nullable=False, server_default="0"))
    op.execute("SELECT crsql_commit_alter('tasks')")

    op.execute(
        "CREATE VIRTUAL TABLE vec_embeddings USING vec0("
        f"vec float[384] distance_metric=cosine INDEXED BY {VEC_INDEX})"
    )
    op.execute(
        "CREATE TRIGGER embeddings_vec_ai AFTER INSERT ON embeddings "
        f"WHEN {VEC_OK} BEGIN "
        "INSERT INTO vec_embeddings(rowid, vec) VALUES (new.rowid, new.vec); END"
    )
    # cr-sqlite merges a new remote row column by column: the row is inserted
    # with vec = x'' (the default) and vec arrives as an UPDATE. DiskANN in
    # sqlite-vec 0.1.10a4 raises "Could not fetch vector data" when deleting a
    # rowid it does not hold, so delete only when the old value was indexed.
    op.execute(
        "CREATE TRIGGER embeddings_vec_au_new AFTER UPDATE OF vec ON embeddings "
        f"WHEN length(old.vec) <> 1536 AND {VEC_OK} BEGIN "
        "INSERT INTO vec_embeddings(rowid, vec) VALUES (new.rowid, new.vec); END"
    )
    op.execute(
        "CREATE TRIGGER embeddings_vec_au_replace AFTER UPDATE OF vec ON embeddings "
        "WHEN length(old.vec) = 1536 BEGIN "
        "DELETE FROM vec_embeddings WHERE rowid = new.rowid; "
        f"INSERT INTO vec_embeddings(rowid, vec) SELECT new.rowid, new.vec WHERE {VEC_OK}; END"
    )
    op.execute(
        "CREATE TRIGGER embeddings_vec_ad AFTER DELETE ON embeddings "
        "WHEN length(old.vec) = 1536 BEGIN "
        "DELETE FROM vec_embeddings WHERE rowid = old.rowid; END"
    )
    op.execute(
        "INSERT INTO vec_embeddings(rowid, vec) "
        "SELECT rowid, vec FROM embeddings WHERE length(vec) = 1536"
    )


def downgrade() -> None:
    for t in ("embeddings_vec_ai", "embeddings_vec_au_new", "embeddings_vec_au_replace", "embeddings_vec_ad"):
        op.execute(f"DROP TRIGGER IF EXISTS {t}")
    op.execute("DROP TABLE IF EXISTS vec_embeddings")
    op.execute("SELECT crsql_begin_alter('tasks')")
    with op.batch_alter_table("tasks") as b:
        b.drop_column("priority")
    op.execute("SELECT crsql_commit_alter('tasks')")
