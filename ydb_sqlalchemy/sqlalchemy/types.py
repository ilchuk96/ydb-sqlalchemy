from sqlalchemy import exc, ColumnElement, ARRAY, types
from sqlalchemy.sql import type_api
from typing import Mapping, Any, Union, Type


class UInt32(types.Integer):
    __visit_name__ = "uint32"


class UInt64(types.Integer):
    __visit_name__ = "uint64"


class UInt8(types.Integer):
    __visit_name__ = "uint8"


class ListType(ARRAY):
    __visit_name__ = "list_type"


class StructType(types.TypeEngine[Mapping[str, Any]]):
    __visit_name__ = "struct_type"

    def __init__(self, fields_types: Mapping[str, Union[Type[types.TypeEngine], Type[types.TypeDecorator]]]):
        self.fields_types = fields_types

    @property
    def python_type(self):
        return dict

    def compare_values(self, x, y):
        return x == y


class Lambda(ColumnElement):
    __visit_name__ = "lambda"

    def __init__(self, func):
        if not callable(func):
            raise exc.ArgumentError("func must be callable")

        self.type = type_api.NULLTYPE
        self.func = func
