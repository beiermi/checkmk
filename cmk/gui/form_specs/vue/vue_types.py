# generated by datamodel-codegen:
#   filename:  vue_types.json
#   timestamp: 2024-03-17T12:37:34+00:00

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Union


@dataclass
class VueBase:
    title: str
    help: str


@dataclass
class VueInteger(VueBase):
    vue_type: str = "integer"
    label: Optional[str] = None
    unit: Optional[str] = None


@dataclass
class VueFloat(VueBase):
    vue_type: str = "float"
    label: Optional[str] = None
    unit: Optional[str] = None


@dataclass
class VueLegacyValuespec(VueBase):
    vue_type: str = "legacy_valuespec"


@dataclass
class VueText(VueBase):
    vue_type: str = "text"
    placeholder: Optional[str] = None


@dataclass
class Model:
    all_schemas: Optional[List[VueSchema]] = None


@dataclass
class VueList(VueBase):
    vue_type: str = "list"
    add_text: Optional[str] = None
    vue_schema: Optional[VueSchema] = None


@dataclass
class VueDictionaryElement:
    ident: str
    required: bool
    default_value: Any
    vue_schema: VueSchema


@dataclass
class VueDictionary(VueBase):
    elements: List[VueDictionaryElement]
    vue_type: str = "dictionary"


VueSchema = Union[VueInteger, VueFloat, VueText, VueDictionary, VueList, VueLegacyValuespec]
