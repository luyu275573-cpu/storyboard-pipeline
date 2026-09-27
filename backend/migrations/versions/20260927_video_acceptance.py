"""Keep video acceptance independent from the image lock."""
import sqlalchemy as sa
from alembic import op

revision = "b742ec319d01"
down_revision = "f6b6201f6705"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shots", sa.Column("accepted_video_attempt_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("shots", "accepted_video_attempt_id")
