import pytest
from sqlalchemy.dialects import registry

registry.register("yql.ydb", "ydb_sqlalchemy.sqlalchemy", "YqlDialect")
registry.register("ydb", "ydb_sqlalchemy.sqlalchemy", "YqlDialect")
pytest.register_assert_rewrite("sqlalchemy.testing.assertions")

from sqlalchemy.testing.plugin.pytestplugin import *  # noqa: E402, F401, F403
