"""
Experimental
Work in progress, breaking changes are possible.
"""
import ydb
import ydb_sqlalchemy.dbapi as dbapi
from ydb_sqlalchemy.dbapi.constants import YDB_KEYWORDS

import sqlalchemy as sa
from sqlalchemy.exc import CompileError, NoSuchTableError
from sqlalchemy.sql import functions, literal_column
from sqlalchemy.sql.compiler import (
    selectable,
    IdentifierPreparer,
    StrSQLTypeCompiler,
    StrSQLCompiler,
    DDLCompiler,
)
from sqlalchemy.sql.elements import ClauseList
from sqlalchemy.engine import reflection
from sqlalchemy.engine.default import StrCompileDialect
from sqlalchemy.util.compat import inspect_getfullargspec

from typing import Any, Optional, Union

from .types import UInt32, UInt64

STR_QUOTE_MAP = {
    "'": "\\'",
    "\\": "\\\\",
    "\0": "\\0",
    "\b": "\\b",
    "\f": "\\f",
    "\r": "\\r",
    "\n": "\\n",
    "\t": "\\t",
    "%": "%%",
}

COMPOUND_KEYWORDS = {
    selectable.CompoundSelect.UNION: "UNION ALL",
    selectable.CompoundSelect.UNION_ALL: "UNION ALL",
    selectable.CompoundSelect.EXCEPT: "EXCEPT",
    selectable.CompoundSelect.EXCEPT_ALL: "EXCEPT ALL",
    selectable.CompoundSelect.INTERSECT: "INTERSECT",
    selectable.CompoundSelect.INTERSECT_ALL: "INTERSECT ALL",
}


class YqlIdentifierPreparer(IdentifierPreparer):
    reserved_words = IdentifierPreparer.reserved_words
    reserved_words.update(YDB_KEYWORDS)

    def __init__(self, dialect):
        super(YqlIdentifierPreparer, self).__init__(
            dialect,
            initial_quote="`",
            final_quote="`",
        )


class YqlTypeCompiler(StrSQLTypeCompiler):
    def visit_CHAR(self, type_, **kw):
        return "UTF8"

    def visit_VARCHAR(self, type_, **kw):
        return "UTF8"

    def visit_unicode(self, type_, **kw):
        return "UTF8"

    def visit_uuid(self, type_, **kw):
        return "UTF8"

    def visit_NVARCHAR(self, type_, **kw):
        return "UTF8"

    def visit_TEXT(self, type_, **kw):
        return "UTF8"

    def visit_FLOAT(self, type_, **kw):
        return "DOUBLE"

    def visit_BOOLEAN(self, type_, **kw):
        return "BOOL"

    def visit_uint32(self, type_, **kw):
        return "UInt32"

    def visit_uint64(self, type_, **kw):
        return "UInt64"

    def visit_uint8(self, type_, **kw):
        return "UInt8"

    def visit_INTEGER(self, type_, **kw):
        return "Int64"

    def visit_NUMERIC(self, type_, **kw):
        """Only Decimal(22,9) is supported for table columns"""
        return f"Decimal({type_.precision}, {type_.scale})"

    def visit_BINARY(self, type_, **kw):
        return "String"

    def visit_BLOB(self, type_, **kw):
        return "String"

    def visit_DATETIME(self, type_, **kw):
        return "Timestamp"


class ParametrizedFunction(functions.Function):
    __visit_name__ = "parametrized_function"

    def __init__(self, name, params, *args, **kwargs):
        super(ParametrizedFunction, self).__init__(name, *args, **kwargs)
        self._func_name = name
        self._func_params = params
        self.params_expr = ClauseList(operator=functions.operators.comma_op, group_contents=True, *params).self_group()


class YqlCompiler(StrSQLCompiler):
    compound_keywords = COMPOUND_KEYWORDS

    def render_bind_cast(self, type_, dbapi_type, sqltext):
        pass

    def group_by_clause(self, select, **kw):
        # Hack to ensure it is possible to define labels in groupby.
        kw.update(within_columns_clause=True)
        return super(YqlCompiler, self).group_by_clause(select, **kw)

    def render_literal_value(self, value, type_):
        if isinstance(value, str):
            value = "".join(STR_QUOTE_MAP.get(x, x) for x in value)
            return f"'{value}'"
        return super().render_literal_value(value, type_)

    def visit_lambda(self, lambda_, **kw):
        func = lambda_.func
        spec = inspect_getfullargspec(func)

        if spec.varargs:
            raise CompileError("Lambdas with *args are not supported")
        if spec.varkw:
            raise CompileError("Lambdas with **kwargs are not supported")

        args = [literal_column("$" + arg) for arg in spec.args]
        text = f'({", ".join("$" + arg for arg in spec.args)}) -> ' f"{{ RETURN {self.process(func(*args), **kw)} ;}}"

        return text

    def visit_parametrized_function(self, func, **kwargs):
        name = func.name
        name_parts = []
        for name in name.split("::"):
            fname = (
                self.preparer.quote(name)
                if self.preparer._requires_quotes_illegal_chars(name) or isinstance(name, sa.sql.elements.quoted_name)
                else name
            )

            name_parts.append(fname)

        name = "::".join(name_parts)
        params = func.params_expr._compiler_dispatch(self, **kwargs)
        args = self.function_argspec(func, **kwargs)
        return "%(name)s%(params)s%(args)s" % dict(name=name, params=params, args=args)

    def visit_function(self, func, add_to_result_map=None, **kwargs):
        # Copypaste of `sa.sql.compiler.SQLCompiler.visit_function` with
        # `::` as namespace separator instead of `.`
        if add_to_result_map:
            add_to_result_map(func.name, func.name, (), func.type)

        disp = getattr(self, f"visit_{func.name.lower()}_func", None)
        if disp:
            return disp(func, **kwargs)

        name = sa.sql.compiler.FUNCTIONS.get(func.__class__)
        if name:
            if func._has_args:
                name += "%(expr)s"
        else:
            name = func.name
            name = (
                self.preparer.quote(name)
                if self.preparer._requires_quotes_illegal_chars(name) or isinstance(name, sa.sql.elements.quoted_name)
                else name
            )
            name += "%(expr)s"

        return "::".join(
            [
                (
                    self.preparer.quote(tok)
                    if self.preparer._requires_quotes_illegal_chars(tok)
                    or isinstance(name, sa.sql.elements.quoted_name)
                    else tok
                )
                for tok in func.packagenames
            ]
            + [name]
        ) % {"expr": self.function_argspec(func, **kwargs)}

    def visit_regexp_match_op_binary(self, binary, operator, **kw):
        return self._generate_generic_binary(binary, " REGEXP ", **kw)

    def visit_not_regexp_match_op_binary(self, binary, operator, **kw):
        return self._generate_generic_binary(binary, " NOT REGEXP ", **kw)

    def _is_optional(self, bind_name: str) -> bool:
        bind = self.binds[bind_name]
        if isinstance(bind.type, sa.Boolean):
            return True
        if bind_name in self.column_keys and hasattr(self.compile_state, "dml_table"):
            if bind_name in self.compile_state.dml_table.c:
                column = self.compile_state.dml_table.c[bind_name]
                return not column.primary_key
        return False

    def _get_bind_type(self, bind_name: str, bind: sa.BindParameter, post_compile_bind_values: list) -> Optional[str]:
        is_optional = self._is_optional(bind_name) or None in post_compile_bind_values
        if not bind.expanding or not isinstance(bind.type, sa.types.NullType):
            bind_type = bind.type.compile(self.dialect)
        else:
            not_null_values = [v for v in post_compile_bind_values if v is not None]
            if not_null_values:
                bind_type = sa.BindParameter("", not_null_values[0]).type.compile(self.dialect)
            else:
                return None
        if is_optional:
            bind_type = f"Optional<{bind_type}>"
        return bind_type

    def render_declare(self, post_compile_parameters: Union[list[dict], dict]) -> str:
        if isinstance(post_compile_parameters, list):
            common_keys = set.intersection(*map(set, post_compile_parameters))
            post_compile_parameters = {k: [dic[k] for dic in post_compile_parameters] for k in common_keys}
        declare_clauses = []
        for bind_name in self.bind_names.values():
            bind = self.binds[bind_name]
            if not bind.literal_execute:
                if not bind.expanding:
                    post_compile_bind_value = post_compile_parameters[bind_name]
                    bind_type = self._get_bind_type(bind_name, bind, [post_compile_bind_value])
                    if bind_type:
                        declare_clauses.append(f"DECLARE %({bind_name})s AS {bind_type};")
                else:
                    post_compile_binds = {k: v for k, v in post_compile_parameters.items() if k.startswith(bind_name)}
                    bind_type = self._get_bind_type(bind_name, bind, list(post_compile_binds.values()))
                    if bind_type:
                        for post_compile_bind_name in post_compile_binds:
                            declare_clauses.append(f"DECLARE %({post_compile_bind_name})s AS {bind_type};")

        return "\n".join(declare_clauses)


class YqlDDLCompiler(DDLCompiler):
    pass


def upsert(table):
    return sa.sql.Insert(table)


COLUMN_TYPES = {
    ydb.PrimitiveType.Int8: sa.INTEGER,
    ydb.PrimitiveType.Int16: sa.INTEGER,
    ydb.PrimitiveType.Int32: sa.INTEGER,
    ydb.PrimitiveType.Int64: sa.INTEGER,
    ydb.PrimitiveType.Uint8: sa.INTEGER,
    ydb.PrimitiveType.Uint16: sa.INTEGER,
    ydb.PrimitiveType.Uint32: UInt32,
    ydb.PrimitiveType.Uint64: UInt64,
    ydb.PrimitiveType.Float: sa.FLOAT,
    ydb.PrimitiveType.Double: sa.FLOAT,
    ydb.PrimitiveType.String: sa.BINARY,
    ydb.PrimitiveType.Utf8: sa.TEXT,
    ydb.PrimitiveType.Json: sa.JSON,
    ydb.PrimitiveType.JsonDocument: sa.JSON,
    ydb.DecimalType: sa.DECIMAL,
    ydb.PrimitiveType.Yson: sa.TEXT,
    ydb.PrimitiveType.Date: sa.DATE,
    ydb.PrimitiveType.Datetime: sa.DATETIME,
    ydb.PrimitiveType.Timestamp: sa.DATETIME,
    ydb.PrimitiveType.Interval: sa.INTEGER,
    ydb.PrimitiveType.Bool: sa.BOOLEAN,
    ydb.PrimitiveType.DyNumber: sa.TEXT,
}


def _get_column_info(t):
    nullable = False
    if isinstance(t, ydb.OptionalType):
        nullable = True
        t = t.item

    if isinstance(t, ydb.DecimalType):
        return sa.DECIMAL(precision=t.precision, scale=t.scale), nullable

    return COLUMN_TYPES[t], nullable


class YqlDialect(StrCompileDialect):
    name = "yql"
    driver = "ydb"

    supports_alter = False
    max_identifier_length = 63
    supports_sane_rowcount = False
    supports_statement_cache = False

    supports_native_enum = False
    supports_native_boolean = True
    supports_native_decimal = True
    supports_smallserial = False
    supports_schemas = False
    supports_constraint_comments = False

    insert_returning = False
    update_returning = False
    delete_returning = False

    supports_sequences = False
    sequences_optional = False
    preexecute_autoincrement_sequences = True
    postfetch_lastrowid = False

    supports_default_values = False
    supports_empty_insert = False
    supports_multivalues_insert = True
    default_paramstyle = "qmark"

    isolation_level = None

    preparer = YqlIdentifierPreparer
    statement_compiler = YqlCompiler
    ddl_compiler = YqlDDLCompiler
    type_compiler = YqlTypeCompiler

    @classmethod
    def import_dbapi(cls: Any):
        return dbapi

    def _describe_table(self, connection, table_name, schema=None):
        if schema is not None:
            raise dbapi.NotSupportedError("unsupported on non empty schema")

        qt = table_name if isinstance(table_name, str) else table_name.name
        raw_conn = connection.connection
        try:
            return raw_conn.describe(qt)
        except dbapi.DatabaseError as e:
            raise NoSuchTableError(qt) from e

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        table = self._describe_table(connection, table_name, schema)
        as_compatible = []
        for column in table.columns:
            col_type, nullable = _get_column_info(column.type)
            as_compatible.append(
                {
                    "name": column.name,
                    "type": col_type,
                    "nullable": nullable,
                    "default": None,
                }
            )

        return as_compatible

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        if schema:
            raise dbapi.NotSupportedError("unsupported on non empty schema")

        driver = connection.connection.driver_connection.driver
        db_path = driver._driver_config.database
        children = driver.scheme_client.list_directory(db_path).children

        return [child.name for child in children if child.is_table()]

    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kwargs):
        try:
            self._describe_table(connection, table_name, schema)
            return True
        except NoSuchTableError:
            return False

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kwargs):
        table = self._describe_table(connection, table_name, schema)
        return {"constrained_columns": table.primary_key, "name": None}

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kwargs):
        # foreign keys unsupported
        return []

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None, **kwargs):
        # TODO: implement me
        return []

    def do_commit(self, dbapi_connection) -> None:
        # TODO: needs to implement?
        pass

    def do_executemany(self, cursor, statement, parameters, context=None):
        if context is not None and not context.isddl and context.compiled:
            statement = context.compiled.render_declare(parameters) + "\n" + statement

        cursor.executemany(statement, parameters)

    def do_execute(self, cursor, statement, parameters, context=None) -> None:
        c = None
        if context is not None:
            if context.isddl:
                c = {"isddl": True}
            elif context.compiled:
                statement = context.compiled.render_declare(parameters) + "\n" + statement

        cursor.execute(statement, parameters, c)
