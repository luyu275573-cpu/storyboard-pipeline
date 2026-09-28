"""Keep the original natural language character description."""

import sqlalchemy as sa
from alembic import op

revision = "d2c7e6a4b901"
down_revision = "b742ec319d01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("characters", sa.Column("source_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("characters", "source_description")
