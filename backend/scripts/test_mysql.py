"""在本地开发容器创建一次性 MySQL 库，验证迁移及全部测试，最终移除本次测试库。"""

import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402

database = f"sbp_test_{uuid.uuid4().hex[:12]}"
url = make_url(settings.database_url)
if url.host != "127.0.0.1" or url.port != 13306 or not re.fullmatch(r"[a-zA-Z0-9_]+", url.username or ""):
    raise SystemExit("此脚本仅用于 compose.dev.yml 的本机开发容器")


def sql(statement):
    command = ["docker", "compose", "--env-file", ".env", "-f", "../deploy/compose.dev.yml", "exec", "-T",
               "mysql", "sh", "-c", 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot']
    result = subprocess.run(command, input=statement, text=True, capture_output=True, cwd=ROOT, timeout=30)
    if result.returncode:
        raise RuntimeError("测试库管理命令失败（已隐藏凭据）")


def run(*args):
    subprocess.run([sys.executable, *args], env=environment, cwd=ROOT, check=True)


environment = {**os.environ, "DATABASE_URL": url.set(database=database).render_as_string(hide_password=False)}
environment["TEST_DATABASE_URL"] = environment["DATABASE_URL"]
environment["PYTHONUTF8"] = "1"
try:
    sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
        f"GRANT ALL ON `{database}`.* TO '{url.username}'@'%';")
    run("-m", "alembic", "upgrade", "head")
    run("-m", "alembic", "check")
    run("-m", "pytest", "--tb=short", "--maxfail=3")
    run("-m", "alembic", "downgrade", "base")
    run("-m", "alembic", "upgrade", "head")
finally:
    # 名称由本次随机生成，从不接受外部传入的删库目标。
    sql(f"DROP DATABASE IF EXISTS `{database}`; REVOKE ALL ON `{database}`.* FROM '{url.username}'@'%';")
    print("Temporary MySQL test database removed.")
