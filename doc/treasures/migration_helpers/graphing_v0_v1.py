#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""
This script migrates legacy graphing objects from given file paths like
  - metric_info[...] = {...}
  - check_metrics[...] = {...}
  - perfometer_info.append(...)
  - graph_info[...] = {...}

The migrated objects will be printed to stdout. It's recommended to use '--debug' in order to see
whether all objects from a file can be migrated. Header, imports, comments or other objects are not
taken into account.
"""

from __future__ import annotations

import argparse
import colorsys
import importlib.util
import logging
import math
import sys
import traceback
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple, TextIO

from cmk.utils.metrics import MetricName

from cmk.gui.graphing._parser import color_to_rgb, RGB  # pylint: disable=cmk-module-layer-violation
from cmk.gui.graphing._perfometer import (  # pylint: disable=cmk-module-layer-violation
    _DualPerfometerSpec,
    _LinearPerfometerSpec,
    _StackedPerfometerSpec,
    LogarithmicPerfometerSpec,
    PerfometerSpec,
)
from cmk.gui.graphing._utils import (  # pylint: disable=cmk-module-layer-violation
    AutomaticDict,
    CheckMetricEntry,
    MetricInfo,
    RawGraphTemplate,
)
from cmk.gui.utils.speaklater import LazyString  # pylint: disable=cmk-module-layer-violation

from cmk.graphing.v1 import graphs, metrics, perfometers, Title, translations

_LOGGER = logging.getLogger(__file__)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        default=False,
        help="Stop at the very first exception",
    )
    parser.add_argument(
        "folders",
        nargs="+",
        help="Search in these folders",
    )
    parser.add_argument(
        "--metric-names",
        nargs="+",
        required=True,
        help="Filter by these metrics names",
    )
    parser.add_argument(
        "--translations",
        action="store_true",
        default=False,
        help="Migrate translations",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Check connected graph objects",
    )
    parser.add_argument(
        "--sanitize",
        action="store_true",
        default=False,
        help="Sanitize connected graph objects, eg. improve colors or similar",
    )
    return parser.parse_args()


def _setup_logger(debug: bool) -> None:
    handler: logging.StreamHandler[TextIO] | logging.NullHandler
    if debug:
        handler = logging.StreamHandler()
    else:
        handler = logging.NullHandler()
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)


def _load_module(filepath: Path) -> types.ModuleType:
    if (spec := importlib.util.spec_from_file_location(f"{filepath.name}", filepath)) is None:
        raise TypeError(spec)
    if (mod := importlib.util.module_from_spec(spec)) is None:
        raise TypeError(mod)
    if spec.loader is None:
        raise TypeError(spec.loader)
    spec.loader.exec_module(mod)
    return mod


def _load_file_content(path: Path) -> tuple[
    Mapping[str, MetricInfo],
    Mapping[str, Mapping[MetricName, CheckMetricEntry]],
    Sequence[PerfometerSpec],
    AutomaticDict,
]:
    module = _load_module(path)
    if hasattr(module, "metric_info"):
        metric_info = module.metric_info
    else:
        metric_info = {}
    if hasattr(module, "check_metrics"):
        check_metrics = module.check_metrics
    else:
        check_metrics = {}
    if hasattr(module, "perfometer_info"):
        perfometer_info = module.perfometer_info
    else:
        perfometer_info = []
    if hasattr(module, "graph_info"):
        graph_info = module.graph_info
    else:
        graph_info = AutomaticDict()
    return metric_info, check_metrics, perfometer_info, graph_info


def _load(debug: bool, folders: Sequence[str], load_translations: bool) -> tuple[
    Mapping[str, MetricInfo],
    Mapping[str, Mapping[MetricName, CheckMetricEntry]],
    Sequence[PerfometerSpec],
    Mapping[str, RawGraphTemplate],
]:
    legacy_metric_info: dict[str, MetricInfo] = {}
    legacy_check_metrics: dict[str, Mapping[MetricName, CheckMetricEntry]] = {}
    legacy_perfometer_info: list[PerfometerSpec] = []
    legacy_graph_info: dict[str, RawGraphTemplate] = {}
    for raw_folder in folders:
        for path in Path(raw_folder).glob("*.py"):
            try:
                (
                    loaded_metric_info,
                    loaded_check_metrics,
                    loaded_perfometer_info,
                    loaded_graph_info,
                ) = _load_file_content(path)
            except Exception:
                if debug:
                    sys.exit(1)

            legacy_metric_info.update(loaded_metric_info)
            if load_translations:
                legacy_check_metrics.update(loaded_check_metrics)
            legacy_perfometer_info.extend(loaded_perfometer_info)
            legacy_graph_info.update(loaded_graph_info)
    return legacy_metric_info, legacy_check_metrics, legacy_perfometer_info, legacy_graph_info


def _show_exception(e: Exception) -> None:
    _LOGGER.error("".join(traceback.format_exception(e)))


#   .--helper--------------------------------------------------------------.
#   |                    _          _                                      |
#   |                   | |__   ___| |_ __   ___ _ __                      |
#   |                   | '_ \ / _ \ | '_ \ / _ \ '__|                     |
#   |                   | | | |  __/ | |_) |  __/ |                        |
#   |                   |_| |_|\___|_| .__/ \___|_|                        |
#   |                                |_|                                   |
#   '----------------------------------------------------------------------'


def _drop_consolidation_func_name(expression: str) -> str:
    if expression.endswith(".max"):
        return expression[:-4]
    if expression.endswith(".min"):
        return expression[:-4]
    if expression.endswith(".average"):
        return expression[:-8]
    return expression


# .
#   .--connected objects---------------------------------------------------.
#   |                                            _           _             |
#   |             ___ ___  _ __  _ __   ___  ___| |_ ___  __| |            |
#   |            / __/ _ \| '_ \| '_ \ / _ \/ __| __/ _ \/ _` |            |
#   |           | (_| (_) | | | | | | |  __/ (__| ||  __/ (_| |            |
#   |            \___\___/|_| |_|_| |_|\___|\___|\__\___|\__,_|            |
#   |                                                                      |
#   |                         _     _           _                          |
#   |                    ___ | |__ (_) ___  ___| |_ ___                    |
#   |                   / _ \| '_ \| |/ _ \/ __| __/ __|                   |
#   |                  | (_) | |_) | |  __/ (__| |_\__ \                   |
#   |                   \___/|_.__// |\___|\___|\__|___/                   |
#   |                            |__/                                      |
#   '----------------------------------------------------------------------'


def _collect_metric_names_of_single_expression(expression: str) -> str:
    expression = _drop_consolidation_func_name(expression)
    if expression.endswith("(%)"):
        expression = expression[:-3]
    if ":" in expression:
        metric_name, _scalar_name = expression.split(":")
        return metric_name
    return expression


@dataclass
class CheckedExpression:
    metric_names: set[str] = field(default_factory=set)
    parseable: bool = True

    def update(self, checked_expression: CheckedExpression) -> None:
        self.metric_names |= checked_expression.metric_names
        if not checked_expression.parseable:
            self.parseable = checked_expression.parseable


def _collect_metric_names_of_expression(expression: str) -> CheckedExpression:
    if "#" in expression:
        expression, _explicit_hexstr_color = expression.rsplit("#", 1)
    if "@" in expression:
        expression, _explicit_unit_name = expression.rsplit("@", 1)
    metric_names = set()
    parseable = True
    for word in expression.split(","):
        match word:
            case "+" | "*" | "-" | "/":
                continue
            case "MIN" | "MAX" | "AVERAGE" | "MERGE" | ">" | ">=" | "<" | "<=":
                parseable = False
                continue
            case _:
                metric_name = _collect_metric_names_of_single_expression(word)
                try:
                    float(metric_name)
                except ValueError:
                    metric_names.add(metric_name)
    return CheckedExpression(metric_names, parseable)


def _collect_metric_names_from_legacy_linear_perfometer(
    legacy_perfometer: _LinearPerfometerSpec,
) -> Iterator[CheckedExpression]:
    for expression in legacy_perfometer["segments"]:
        yield _collect_metric_names_of_expression(expression)
    if (total := legacy_perfometer.get("total")) and isinstance(total, str):
        yield _collect_metric_names_of_expression(total)
    if condition := legacy_perfometer.get("condition"):
        checked_expression = _collect_metric_names_of_expression(condition)
        yield CheckedExpression(checked_expression.metric_names, False)
    if label := legacy_perfometer.get("label"):
        yield _collect_metric_names_of_expression(label[0])


def _collect_metric_names_from_legacy_logarithmic_perfometer(
    legacy_perfometer: LogarithmicPerfometerSpec,
) -> CheckedExpression:
    return _collect_metric_names_of_expression(legacy_perfometer["metric"])


def _collect_metric_names_from_legacy_perfometer(
    legacy_perfometer: PerfometerSpec,
) -> CheckedExpression:
    checked_expression = CheckedExpression()
    if legacy_perfometer["type"] == "linear":
        for pe in _collect_metric_names_from_legacy_linear_perfometer(legacy_perfometer):
            checked_expression.update(pe)
    elif legacy_perfometer["type"] == "logarithmic":
        checked_expression.update(
            _collect_metric_names_from_legacy_logarithmic_perfometer(legacy_perfometer)
        )
    elif legacy_perfometer["type"] in ("dual", "stacked"):
        for p in legacy_perfometer["perfometers"]:
            checked_expression.update(_collect_metric_names_from_legacy_perfometer(p))
    return checked_expression


def _collect_metric_names_from_legacy_graph(
    legacy_graph: RawGraphTemplate,
) -> CheckedExpression:
    checked_expression = CheckedExpression()
    for scalar in legacy_graph.get("scalars", []):
        if isinstance(scalar, tuple):
            checked_expression.update(_collect_metric_names_of_expression(scalar[0]))
        else:
            checked_expression.update(_collect_metric_names_of_expression(scalar))
    for metric in legacy_graph["metrics"]:
        checked_expression.update(_collect_metric_names_of_expression(metric[0]))
    for boundary in legacy_graph.get("range", []):
        if isinstance(boundary, str):
            checked_expression.update(_collect_metric_names_of_expression(boundary))
    if optional_metrics := legacy_graph.get("optional_metrics", []):
        checked_expression.update(CheckedExpression(set(optional_metrics), True))
    if conflicting_metrics := legacy_graph.get("conflicting_metrics", []):
        checked_expression.update(CheckedExpression(set(conflicting_metrics), True))
    return checked_expression


@dataclass(frozen=True)
class CheckedPerfometer:
    index: int
    parseable: bool


@dataclass(frozen=True)
class CheckedGraphTemplate:
    ident: str
    parseable: bool


@dataclass(frozen=True)
class GraphingObjectReferences:
    perfometer_indices: set[int] = field(default_factory=set)
    graph_template_ids: set[str] = field(default_factory=set)


def _find_connected_metric_names(
    metric_name: str,
    handled_metric_names: set[str],
    checked_perfometers: Mapping[int, CheckedExpression],
    checked_graph_templates: Mapping[str, CheckedExpression],
    used_metrics: Mapping[str, GraphingObjectReferences],
) -> Iterator[str]:
    if metric_name in handled_metric_names:
        return

    handled_metric_names.add(metric_name)
    references = used_metrics[metric_name]
    connected_metric_names = set()
    for idx in references.perfometer_indices:
        connected_metric_names |= checked_perfometers[idx].metric_names

    for ident in references.graph_template_ids:
        connected_metric_names |= checked_graph_templates[ident].metric_names

    for connected_metric_name in connected_metric_names:
        yield connected_metric_name
        yield from _find_connected_metric_names(
            connected_metric_name,
            handled_metric_names,
            checked_perfometers,
            checked_graph_templates,
            used_metrics,
        )


@dataclass(frozen=True)
class ConnectedPerfometer:
    idx: int
    spec: PerfometerSpec
    parseable: bool


@dataclass(frozen=True)
class ConnectedGraphTemplate:
    ident: str
    template: RawGraphTemplate
    parseable: bool


@dataclass(frozen=True)
class ConnectedObjects:
    metrics: Mapping[str, MetricInfo]
    perfometers: Sequence[ConnectedPerfometer]
    graph_templates: Sequence[ConnectedGraphTemplate]


def _make_connected_objects(
    metric_names_filter: Sequence[str],
    legacy_metric_info: Mapping[str, MetricInfo],
    legacy_perfometer_info: Sequence[PerfometerSpec],
    legacy_graph_info: Mapping[str, RawGraphTemplate],
    used_metrics: Mapping[str, GraphingObjectReferences],
    checked_perfometers: Mapping[int, CheckedExpression],
    checked_graph_templates: Mapping[str, CheckedExpression],
) -> Iterator[ConnectedObjects]:
    handled_metric_names: set[str] = set()
    all_connected_metric_names: set[tuple[str, ...]] = set()
    for metric_name in used_metrics:
        if metric_name not in metric_names_filter:
            continue
        all_connected_metric_names.add(
            tuple(
                sorted(
                    set(
                        _find_connected_metric_names(
                            metric_name,
                            handled_metric_names,
                            checked_perfometers,
                            checked_graph_templates,
                            used_metrics,
                        )
                    )
                )
            )
        )

    for connected_metric_names in all_connected_metric_names:
        perfometers_: list[ConnectedPerfometer] = []
        graph_templates_: list[ConnectedGraphTemplate] = []
        for metric_name in connected_metric_names:
            references = used_metrics[metric_name]
            for idx in references.perfometer_indices:
                connected_perfometer = ConnectedPerfometer(
                    idx,
                    legacy_perfometer_info[idx],
                    checked_perfometers[idx].parseable,
                )
                if connected_perfometer not in perfometers_:
                    perfometers_.append(connected_perfometer)

            for ident in references.graph_template_ids:
                connected_graph_template = ConnectedGraphTemplate(
                    ident,
                    legacy_graph_info[ident],
                    checked_graph_templates[ident].parseable,
                )
                if connected_graph_template not in graph_templates_:
                    graph_templates_.append(connected_graph_template)

        yield ConnectedObjects(
            {c: legacy_metric_info[c] for c in connected_metric_names},
            perfometers_,
            graph_templates_,
        )


def _compute_connected_objects(
    debug: bool,
    folders: Sequence[str],
    metric_names_filter: Sequence[str],
    legacy_metric_info: Mapping[str, MetricInfo],
    legacy_perfometer_info: Sequence[PerfometerSpec],
    legacy_graph_info: Mapping[str, RawGraphTemplate],
) -> Sequence[ConnectedObjects]:
    checked_perfometers: dict[int, CheckedExpression] = {}
    checked_graph_templates: dict[str, CheckedExpression] = {}
    used_metrics: dict[str, GraphingObjectReferences] = {}

    for idx, legacy_perfometer in enumerate(legacy_perfometer_info):
        checked_expression = _collect_metric_names_from_legacy_perfometer(legacy_perfometer)
        checked_perfometers.setdefault(idx, checked_expression)
        for metric_name in checked_expression.metric_names:
            used_metrics.setdefault(metric_name, GraphingObjectReferences()).perfometer_indices.add(
                idx
            )

    for ident, template in legacy_graph_info.items():
        checked_expression = _collect_metric_names_from_legacy_graph(template)
        checked_graph_templates.setdefault(ident, checked_expression)
        for metric_name in checked_expression.metric_names:
            used_metrics.setdefault(metric_name, GraphingObjectReferences()).graph_template_ids.add(
                ident
            )

    return list(
        _make_connected_objects(
            metric_names_filter,
            legacy_metric_info,
            legacy_perfometer_info,
            legacy_graph_info,
            used_metrics,
            checked_perfometers,
            checked_graph_templates,
        )
    )


# .
#   .--migrate-------------------------------------------------------------.
#   |                           _                 _                        |
#   |                 _ __ ___ (_) __ _ _ __ __ _| |_ ___                  |
#   |                | '_ ` _ \| |/ _` | '__/ _` | __/ _ \                 |
#   |                | | | | | | | (_| | | | (_| | ||  __/                 |
#   |                |_| |_| |_|_|\__, |_|  \__,_|\__\___|                 |
#   |                             |___/                                    |
#   '----------------------------------------------------------------------'


@dataclass(frozen=True)
class Unparseable:
    namespace: Literal["metrics", "translations", "perfometers", "graphs"]
    name: str


class ParsedUnit(NamedTuple):
    name: str
    unit: metrics.Unit


_UNIT_MAP = {
    "": ParsedUnit(
        "UNIT_NUMBER",
        metrics.Unit(metrics.DecimalNotation("")),
    ),
    "count": ParsedUnit(
        "UNIT_COUNTER",
        metrics.Unit(metrics.DecimalNotation(""), metrics.StrictPrecision(2)),
    ),
    "%": ParsedUnit(
        "UNIT_PERCENTAGE",
        metrics.Unit(metrics.DecimalNotation("%")),
    ),
    "s": ParsedUnit(
        "UNIT_TIME",
        metrics.Unit(metrics.TimeNotation()),
    ),
    "1/s": ParsedUnit(
        "UNIT_PER_SECOND",
        metrics.Unit(metrics.DecimalNotation("/s")),
    ),
    "hz": ParsedUnit(
        "UNIT_HERTZ",
        metrics.Unit(metrics.DecimalNotation("Hz")),
    ),
    "bytes": ParsedUnit(
        "UNIT_BYTES",
        metrics.Unit(metrics.IECNotation("B")),
    ),
    "bytes/s": ParsedUnit(
        "UNIT_BYTES_PER_SECOND",
        metrics.Unit(metrics.IECNotation("B/s")),
    ),
    "s/s": ParsedUnit(
        "UNIT_SECONDS_PER_SECOND",
        metrics.Unit(metrics.DecimalNotation("s/s")),
    ),
    "bits": ParsedUnit(
        "UNIT_BITS",
        metrics.Unit(metrics.IECNotation("bits/s")),
    ),
    "bits/s": ParsedUnit(
        "UNIT_BITS_PER_SECOND",
        metrics.Unit(metrics.IECNotation("bits/d")),
    ),
    "bytes/d": ParsedUnit(
        "UNIT_BYTES_PER_DAY",
        metrics.Unit(metrics.IECNotation("B/d")),
    ),
    "c": ParsedUnit(
        "UNIT_DEGREE_CELSIUS",
        metrics.Unit(metrics.DecimalNotation("°C")),
    ),
    "a": ParsedUnit(
        "UNIT_AMPERE",
        metrics.Unit(metrics.DecimalNotation("A"), metrics.AutoPrecision(3)),
    ),
    "v": ParsedUnit(
        "UNIT_VOLTAGE",
        metrics.Unit(metrics.DecimalNotation("V"), metrics.AutoPrecision(3)),
    ),
    "w": ParsedUnit(
        "UNIT_ELECTRICAL_POWER",
        metrics.Unit(metrics.DecimalNotation("W"), metrics.AutoPrecision(3)),
    ),
    "va": ParsedUnit(
        "UNIT_ELECTRICAL_APPARENT_POWER",
        metrics.Unit(metrics.DecimalNotation("VA"), metrics.AutoPrecision(3)),
    ),
    "wh": ParsedUnit(
        "UNIT_ELECTRICAL_ENERGY",
        metrics.Unit(metrics.DecimalNotation("Wh"), metrics.AutoPrecision(3)),
    ),
    "dbm": ParsedUnit(
        "UNIT_DECIBEL_MILLIWATTS",
        metrics.Unit(metrics.DecimalNotation("dBm")),
    ),
    "dbmv": ParsedUnit(
        "UNIT_DECIBEL_MILLIVOLTS",
        metrics.Unit(metrics.DecimalNotation("dBmV")),
    ),
    "db": ParsedUnit(
        "UNIT_DECIBEL",
        metrics.Unit(metrics.DecimalNotation("dB")),
    ),
    "ppm": ParsedUnit(
        "UNIT_PARTS_PER_MILLION",
        metrics.Unit(metrics.DecimalNotation("ppm")),
    ),
    "%/m": ParsedUnit(
        "UNIT_PERCENTAGE_PER_METER",
        metrics.Unit(metrics.DecimalNotation("%/m")),
    ),
    "bar": ParsedUnit(
        "UNIT_BAR",
        metrics.Unit(metrics.DecimalNotation("bar"), metrics.AutoPrecision(4)),
    ),
    "pa": ParsedUnit(
        "UNIT_PASCAL",
        metrics.Unit(metrics.DecimalNotation("Pa"), metrics.AutoPrecision(3)),
    ),
    "l/s": ParsedUnit(
        "UNIT_LITER_PER_SECOND",
        metrics.Unit(metrics.DecimalNotation("l/s"), metrics.AutoPrecision(3)),
    ),
    "rpm": ParsedUnit(
        "UNIT_REVOLUTIONS_PER_MINUTE",
        metrics.Unit(metrics.DecimalNotation("rpm"), metrics.AutoPrecision(4)),
    ),
    "bytes/op": ParsedUnit(
        "UNIT_BYTES_PER_OPERATION",
        metrics.Unit(metrics.IECNotation("B/op")),
    ),
    "EUR": ParsedUnit(
        "UNIT_EURO",
        metrics.Unit(metrics.DecimalNotation("€"), metrics.StrictPrecision(2)),
    ),
    "RCU": ParsedUnit(
        "UNIT_READ_CAPACITY_UNIT",
        metrics.Unit(metrics.SINotation("RCU"), metrics.AutoPrecision(3)),
    ),
    "WCU": ParsedUnit(
        "UNIT_WRITE_CAPACITY_UNIT",
        metrics.Unit(metrics.SINotation("WCU"), metrics.AutoPrecision(3)),
    ),
}


@dataclass(frozen=True)
class UnitParser:
    _units: set[ParsedUnit] = field(default_factory=set)

    @property
    def units(self) -> Sequence[ParsedUnit]:
        return list(self._units)

    def find_unit_name(self, unit: metrics.Unit) -> str:
        for u in self.units:
            if u.unit == unit:
                return u.name
        return self.default.name

    @property
    def default(self) -> ParsedUnit:
        parsed = _UNIT_MAP[""]
        self._units.add(parsed)
        return parsed

    def parse(self, legacy_unit: str) -> metrics.Unit:
        if legacy_unit in _UNIT_MAP:
            parsed = _UNIT_MAP[legacy_unit]
        else:
            _LOGGER.info("Unit %r not found, use 'DecimalUnit'", legacy_unit)
            parsed = self.default
        self._units.add(parsed)
        return parsed.unit


def _rgb_from_hexstr(hexstr: str) -> RGB:
    return RGB(*(int(hexstr.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)))


_LEGACY_COLOR_WHEEL = {
    "11": (0.775, 1, 1),
    "12": (0.8, 1, 1),
    "13": (0.83, 1, 1),
    "14": (0.05, 1, 1),
    "15": (0.08, 1, 1),
    "16": (0.105, 1, 1),
    # yellow area
    "21": (0.13, 1, 1),
    "22": (0.14, 1, 1),
    "23": (0.155, 1, 1),
    "24": (0.185, 1, 1),
    "25": (0.21, 1, 1),
    "26": (0.25, 1, 1),
    # green area
    "31": (0.45, 1, 1),
    "32": (0.5, 1, 1),
    "33": (0.515, 1, 1),
    "34": (0.53, 1, 1),
    "35": (0.55, 1, 1),
    "36": (0.57, 1, 1),
    # blue area
    "41": (0.59, 1, 1),
    "42": (0.62, 1, 1),
    "43": (0.66, 1, 1),
    "44": (0.71, 1, 1),
    "45": (0.73, 1, 1),
    "46": (0.75, 1, 1),
    # special colors
    "51": (0, 0, 0.5),
    "52": (0.067, 0.7, 0.5),
    "53": (0.083, 0.8, 0.55),
}


def _rgb_from_legacy_wheel(legacy_color_name: str) -> RGB:
    name, nuance = legacy_color_name.split("/", 1)
    hsv = _LEGACY_COLOR_WHEEL[name]
    if nuance == "b":
        factors = (1.0, 1.0, 0.8) if name[0] in ["2", "3"] else (1.0, 0.6, 1.0)
        hsv = (hsv[0] * factors[0], hsv[1] * factors[1], hsv[2] * factors[2])
    rgb = colorsys.hsv_to_rgb(*hsv)
    return RGB(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))


@dataclass(frozen=True)
class _Distance:
    distance: float
    color: metrics.Color

    @classmethod
    def from_rgb(cls, legacy_rgb: RGB, new: metrics.Color) -> _Distance:
        new_rgb = color_to_rgb(new)
        return cls(
            math.sqrt(
                pow(legacy_rgb.red - new_rgb.red, 2)
                + pow(legacy_rgb.green - new_rgb.green, 2)
                + pow(legacy_rgb.blue - new_rgb.blue, 2)
            ),
            new,
        )


def _parse_legacy_metric_info(
    unit_parser: UnitParser, name: str, info: MetricInfo
) -> metrics.Metric:
    if (legacy_color := info["color"]).startswith("#"):
        rgb = _rgb_from_hexstr(legacy_color)
    else:
        rgb = _rgb_from_legacy_wheel(legacy_color)
    return metrics.Metric(
        name=name,
        title=Title(str(info["title"])),
        unit=unit_parser.parse(info["unit"]),
        color=min(
            (_Distance.from_rgb(rgb, color) for color in metrics.Color),
            key=lambda d: d.distance,
        ).color,
    )


def _parse_legacy_metric_infos(
    debug: bool,
    unparseables: list[Unparseable],
    unit_parser: UnitParser,
    metric_info: Mapping[str, MetricInfo],
) -> Iterator[metrics.Metric]:
    for name, info in metric_info.items():
        try:
            yield _parse_legacy_metric_info(unit_parser, name, info)
        except Exception as e:
            _show_exception(e)
            if debug:
                raise e
            unparseables.append(Unparseable("metrics", name))


def _parse_legacy_check_metrics(
    debug: bool,
    unparseables: list[Unparseable],
    check_metrics: Mapping[str, Mapping[MetricName, CheckMetricEntry]],
) -> Iterator[translations.Translation]:
    by_translations: dict[
        tuple[
            tuple[
                str,
                translations.RenameTo | translations.ScaleBy | translations.RenameToAndScaleBy,
            ],
            ...,
        ],
        list[
            translations.PassiveCheck
            | translations.ActiveCheck
            | translations.HostCheckCommand
            | translations.NagiosPlugin
        ],
    ] = {}
    for name, info in check_metrics.items():
        check_command: (
            translations.PassiveCheck
            | translations.ActiveCheck
            | translations.HostCheckCommand
            | translations.NagiosPlugin
        )
        if name.startswith("check_mk-"):
            check_command = translations.PassiveCheck(name[9:])
        elif name.startswith("check_mk_active-"):
            check_command = translations.ActiveCheck(name[16:])
        elif name.startswith("check-mk-"):
            check_command = translations.HostCheckCommand(name[9:])
        elif name.startswith("check_"):
            check_command = translations.NagiosPlugin(name[6:])
        else:
            unparseables.append(Unparseable("translations", name))
            raise ValueError(name)

        translations_: list[
            tuple[
                str,
                translations.RenameTo | translations.ScaleBy | translations.RenameToAndScaleBy,
            ]
        ] = []
        for legacy_name, attrs in info.items():
            match "name" in attrs, "scale" in attrs:
                case True, True:
                    translations_.append(
                        (
                            legacy_name,
                            translations.RenameToAndScaleBy(attrs["name"], attrs["scale"]),
                        )
                    )
                case True, False:
                    translations_.append((legacy_name, translations.RenameTo(attrs["name"])))
                case False, True:
                    translations_.append((legacy_name, translations.ScaleBy(attrs["scale"])))
                case _:
                    continue

        by_translations.setdefault(tuple(sorted(translations_, key=lambda t: t[0])), []).append(
            check_command
        )

    for sorted_translations, check_commands in by_translations.items():
        name = "_".join([c.name for c in check_commands])
        try:
            yield translations.Translation(
                name=name,
                check_commands=check_commands,
                translations=dict(sorted_translations),
            )
        except Exception as e:
            _show_exception(e)
            if debug:
                raise e
            unparseables.append(Unparseable("translations", name))


_Operators = Literal["+", "*", "-", "/"]


def _parse_scalar_name(
    scalar_name: str, metric_name: str
) -> metrics.WarningOf | metrics.CriticalOf | metrics.MinimumOf | metrics.MaximumOf:
    match scalar_name:
        case "warn":
            return metrics.WarningOf(metric_name)
        case "crit":
            return metrics.CriticalOf(metric_name)
        case "min":
            return metrics.MinimumOf(metric_name, color=metrics.Color.GRAY)
        case "max":
            return metrics.MaximumOf(metric_name, color=metrics.Color.GRAY)
    raise ValueError(scalar_name)


def _make_percent(
    unit_parser: UnitParser,
    percent_value: (
        str | metrics.WarningOf | metrics.CriticalOf | metrics.MinimumOf | metrics.MaximumOf
    ),
    metric_name: str,
    explicit_title: str,
    explicit_color: metrics.Color,
) -> metrics.Fraction:
    return metrics.Fraction(
        Title(explicit_title),
        metrics.Unit(metrics.DecimalNotation("%")),
        explicit_color,
        dividend=metrics.Product(
            # Title, unit, color have no impact
            Title(""),
            unit_parser.default.unit,
            metrics.Color.GRAY,
            [
                metrics.Constant(
                    # Title, unit, color have no impact
                    Title(""),
                    unit_parser.default.unit,
                    metrics.Color.GRAY,
                    100.0,
                ),
                percent_value,
            ],
        ),
        divisor=metrics.MaximumOf(
            # Color has no impact
            metric_name,
            color=metrics.Color.GRAY,
        ),
    )


def _parse_constant_or_metric_name(expression: str) -> str | metrics.Constant:
    try:
        return metrics.Constant(
            Title(""),
            metrics.Unit(metrics.DecimalNotation("")),
            metrics.Color.BLUE,
            float(expression),
        )
    except ValueError:
        return expression


def _parse_single_expression(
    unit_parser: UnitParser, expression: str, explicit_title: str, explicit_color: metrics.Color
) -> (
    str
    | metrics.Constant
    | metrics.WarningOf
    | metrics.CriticalOf
    | metrics.MinimumOf
    | metrics.MaximumOf
    | metrics.Fraction
):
    expression = _drop_consolidation_func_name(expression)
    if percent := expression.endswith("(%)"):
        expression = expression[:-3]

    if ":" in expression:
        metric_name, scalar_name = expression.split(":")
        scalar = _parse_scalar_name(scalar_name, metric_name)
        return (
            _make_percent(unit_parser, scalar, metric_name, explicit_title, explicit_color)
            if percent
            else scalar
        )

    return (
        _make_percent(unit_parser, expression, expression, explicit_title, explicit_color)
        if percent
        else _parse_constant_or_metric_name(expression)
    )


def _resolve_stack(
    unit_parser: UnitParser,
    stack: Sequence[
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
        | _Operators
    ],
    explicit_title: str,
    explicit_unit_name: str,
    explicit_color: metrics.Color,
) -> (
    str
    | metrics.Constant
    | metrics.WarningOf
    | metrics.CriticalOf
    | metrics.MinimumOf
    | metrics.MaximumOf
    | metrics.Sum
    | metrics.Product
    | metrics.Difference
    | metrics.Fraction
):
    resolved: list[
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ] = []
    for element in stack:
        if (isinstance(element, str) and element not in ("+", "*", "-", "/")) or isinstance(
            element,
            (
                metrics.Constant,
                metrics.WarningOf,
                metrics.CriticalOf,
                metrics.MinimumOf,
                metrics.MaximumOf,
                metrics.Sum,
                metrics.Product,
                metrics.Difference,
                metrics.Fraction,
            ),
        ):
            resolved.append(element)
            continue

        right = resolved.pop()
        left = resolved.pop()

        match element:
            case "+":
                resolved.append(
                    metrics.Sum(
                        Title(explicit_title),
                        explicit_color,
                        [left, right],
                    )
                )
            case "*":
                resolved.append(
                    metrics.Product(
                        Title(explicit_title),
                        unit_parser.parse(explicit_unit_name),
                        explicit_color,
                        [left, right],
                    )
                )
            case "-":
                resolved.append(
                    metrics.Difference(
                        Title(explicit_title),
                        explicit_color,
                        minuend=left,
                        subtrahend=right,
                    )
                )
            case "/":
                # Handle zero division by always adding a tiny bit to the divisor
                resolved.append(
                    metrics.Fraction(
                        Title(explicit_title),
                        unit_parser.parse(explicit_unit_name),
                        explicit_color,
                        dividend=left,
                        divisor=metrics.Sum(
                            # Title, color have no impact
                            Title(""),
                            metrics.Color.GRAY,
                            [
                                right,
                                metrics.Constant(
                                    # Title, unit, color have no impact
                                    Title(""),
                                    unit_parser.default.unit,
                                    metrics.Color.GRAY,
                                    1e-16,
                                ),
                            ],
                        ),
                    )
                )

    return resolved[0]


def _parse_expression(
    unit_parser: UnitParser, expression: str, explicit_title: str
) -> (
    str
    | metrics.Constant
    | metrics.WarningOf
    | metrics.CriticalOf
    | metrics.MinimumOf
    | metrics.MaximumOf
    | metrics.Sum
    | metrics.Product
    | metrics.Difference
    | metrics.Fraction
):
    if "#" in expression:
        expression, explicit_hexstr_color = expression.rsplit("#", 1)
        explicit_color = min(
            (
                _Distance.from_rgb(_rgb_from_hexstr(f"#{explicit_hexstr_color}"), color)
                for color in metrics.Color
            ),
            key=lambda d: d.distance,
        ).color
    else:
        explicit_color = metrics.Color.GRAY

    explicit_unit_name = ""
    if "@" in expression:
        expression, explicit_unit_name = expression.rsplit("@", 1)

    stack: list[
        _Operators
        | str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ] = []
    for word in expression.split(","):
        match word:
            case "+":
                stack.append("+")
            case "*":
                stack.append("*")
            case "-":
                stack.append("-")
            case "/":
                stack.append("/")
            case "MIN" | "MAX" | "AVERAGE" | "MERGE" | ">" | ">=" | "<" | "<=":
                raise ValueError(word)
            case _:
                stack.append(
                    _parse_single_expression(unit_parser, word, explicit_title, explicit_color)
                )

    return _resolve_stack(unit_parser, stack, explicit_title, explicit_unit_name, explicit_color)


def _raw_metric_names(
    quantity: (
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ),
) -> Iterator[str]:
    match quantity:
        case str():
            yield quantity
        case metrics.WarningOf() | metrics.CriticalOf() | metrics.MinimumOf() | metrics.MaximumOf():
            yield quantity.metric_name
        case metrics.Sum():
            for s in quantity.summands:
                yield from _raw_metric_names(s)
        case metrics.Product():
            for f in quantity.factors:
                yield from _raw_metric_names(f)
        case metrics.Difference():
            yield from _raw_metric_names(quantity.minuend)
            yield from _raw_metric_names(quantity.subtrahend)
        case metrics.Fraction():
            yield from _raw_metric_names(quantity.dividend)
            yield from _raw_metric_names(quantity.divisor)


def _perfometer_name(
    segments: Sequence[
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ],
) -> str:
    return "_".join([n for s in segments for n in _raw_metric_names(s)])


def _parse_legacy_linear_perfometer(
    unit_parser: UnitParser,
    legacy_linear_perfometer: _LinearPerfometerSpec,
) -> perfometers.Perfometer:
    if "condition" in legacy_linear_perfometer:
        # Note: there are perfometers with 'condition' which exclude each other.
        # We have to migrate them manually.
        raise ValueError("condition")

    if "label" in legacy_linear_perfometer:
        _LOGGER.info("Perfometer field 'label' will not be migrated")

    legacy_total = legacy_linear_perfometer["total"]
    segments = [_parse_expression(unit_parser, s, "") for s in legacy_linear_perfometer["segments"]]
    return perfometers.Perfometer(
        name=_perfometer_name(segments),
        focus_range=perfometers.FocusRange(
            perfometers.Closed(0),
            perfometers.Closed(
                legacy_total
                if isinstance(legacy_total, (int, float))
                else _parse_expression(unit_parser, legacy_total, "")
            ),
        ),
        segments=segments,
    )


def _compute_border85(half_value: int | float) -> int:
    border85 = int((85.0 * half_value) / 50)
    power = pow(10, math.floor(math.log10(border85)))
    return math.ceil(border85 / power) * power


def _parse_legacy_logarithmic_perfometer(
    unit_parser: UnitParser,
    legacy_logarithmic_perfometer: LogarithmicPerfometerSpec,
) -> perfometers.Perfometer:
    segments = [_parse_expression(unit_parser, legacy_logarithmic_perfometer["metric"], "")]
    return perfometers.Perfometer(
        name=_perfometer_name(segments),
        focus_range=perfometers.FocusRange(
            perfometers.Closed(0),
            perfometers.Open(_compute_border85(legacy_logarithmic_perfometer["half_value"])),
        ),
        segments=segments,
    )


def _parse_legacy_dual_perfometer(
    unit_parser: UnitParser,
    legacy_dual_perfometer: _DualPerfometerSpec,
) -> perfometers.Bidirectional:
    legacy_left, legacy_right = legacy_dual_perfometer["perfometers"]

    if legacy_left["type"] == "linear":
        left = _parse_legacy_linear_perfometer(unit_parser, legacy_left)
    elif legacy_left["type"] == "logarithmic":
        left = _parse_legacy_logarithmic_perfometer(unit_parser, legacy_left)
    else:
        raise ValueError(legacy_left)

    if legacy_right["type"] == "linear":
        right = _parse_legacy_linear_perfometer(unit_parser, legacy_right)
    elif legacy_right["type"] == "logarithmic":
        right = _parse_legacy_logarithmic_perfometer(unit_parser, legacy_right)
    else:
        raise ValueError(legacy_right)

    return perfometers.Bidirectional(
        name=f"{left.name}_{right.name}",
        left=left,
        right=right,
    )


def _parse_legacy_stacked_perfometer(
    unit_parser: UnitParser,
    legacy_stacked_perfometer: _StackedPerfometerSpec,
) -> perfometers.Stacked:
    legacy_upper, legacy_lower = legacy_stacked_perfometer["perfometers"]

    if legacy_upper["type"] == "linear":
        upper = _parse_legacy_linear_perfometer(unit_parser, legacy_upper)
    elif legacy_upper["type"] == "logarithmic":
        upper = _parse_legacy_logarithmic_perfometer(unit_parser, legacy_upper)
    else:
        raise ValueError(legacy_upper)

    if legacy_lower["type"] == "linear":
        lower = _parse_legacy_linear_perfometer(unit_parser, legacy_lower)
    elif legacy_lower["type"] == "logarithmic":
        lower = _parse_legacy_logarithmic_perfometer(unit_parser, legacy_lower)
    else:
        raise ValueError(legacy_lower)

    return perfometers.Stacked(
        name=f"{lower.name}_{upper.name}",
        lower=lower,
        upper=upper,
    )


def _parse_legacy_perfometer_infos(
    debug: bool,
    unparseables: list[Unparseable],
    unit_parser: UnitParser,
    perfometer_info: Sequence[PerfometerSpec],
) -> Iterator[perfometers.Perfometer | perfometers.Bidirectional | perfometers.Stacked]:
    for idx, info in enumerate(perfometer_info):
        try:
            if info["type"] == "linear":
                yield _parse_legacy_linear_perfometer(unit_parser, info)
            elif info["type"] == "logarithmic":
                yield _parse_legacy_logarithmic_perfometer(unit_parser, info)
            elif info["type"] == "dual":
                yield _parse_legacy_dual_perfometer(unit_parser, info)
            elif info["type"] == "stacked":
                yield _parse_legacy_stacked_perfometer(unit_parser, info)
        except Exception as e:
            _show_exception(e)
            if debug:
                raise e
            unparseables.append(Unparseable("perfometers", str(idx)))


def _parse_legacy_metric(
    unit_parser: UnitParser, legacy_metric: tuple[str, str] | tuple[str, str, str]
) -> (
    str
    | metrics.Constant
    | metrics.WarningOf
    | metrics.CriticalOf
    | metrics.MinimumOf
    | metrics.MaximumOf
    | metrics.Sum
    | metrics.Product
    | metrics.Difference
    | metrics.Fraction
):
    expression, _line_type, *rest = legacy_metric
    return _parse_expression(unit_parser, expression, str(rest[0]) if rest else "")


def _parse_legacy_metrics(
    unit_parser: UnitParser, legacy_metrics: Sequence
) -> tuple[Sequence, Sequence, Sequence, Sequence]:
    lower_compound_lines = []
    lower_simple_lines = []
    upper_compound_lines = []
    upper_simple_lines = []
    for legacy_metric in legacy_metrics:
        migrated_metric = _parse_legacy_metric(unit_parser, legacy_metric)
        match legacy_metric[1]:
            case "-line":
                lower_simple_lines.append(migrated_metric)
            case "-area":
                lower_compound_lines.append(migrated_metric)
            case "-stack":
                lower_compound_lines.append(migrated_metric)
            case "line":
                upper_simple_lines.append(migrated_metric)
            case "area":
                upper_compound_lines.append(migrated_metric)
            case "stack":
                upper_compound_lines.append(migrated_metric)
            case _:
                raise ValueError(legacy_metric)

    return lower_compound_lines, lower_simple_lines, upper_compound_lines, upper_simple_lines


def _parse_legacy_scalars(
    unit_parser: UnitParser,
    legacy_scalars: Sequence[str | tuple[str, str | LazyString]],
) -> Iterator[
    str
    | metrics.Constant
    | metrics.WarningOf
    | metrics.CriticalOf
    | metrics.MinimumOf
    | metrics.MaximumOf
    | metrics.Sum
    | metrics.Product
    | metrics.Difference
    | metrics.Fraction
]:
    for legacy_scalar in legacy_scalars:
        if isinstance(legacy_scalar, str):
            yield _parse_expression(unit_parser, legacy_scalar, "")
        elif isinstance(legacy_scalar, tuple):
            yield _parse_expression(unit_parser, legacy_scalar[0], str(legacy_scalar[1]))
        else:
            raise ValueError(legacy_scalar)


def _parse_lower_or_upper_scalars(
    scalars: Sequence[
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ],
) -> tuple[
    Sequence[
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ],
    Sequence[
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ],
]:
    lower_scalars = []
    upper_scalars = []
    for s in scalars:
        if (
            isinstance(s, metrics.Product)
            and len(s.factors) == 2
            and any(isinstance(f, metrics.Constant) and f.value == -1 for f in s.factors)
        ):
            for f in s.factors:
                if not isinstance(f, metrics.Constant):
                    lower_scalars.append(f)
        else:
            upper_scalars.append(s)
    return lower_scalars, upper_scalars


def _parse_legacy_range(
    unit_parser: UnitParser,
    legacy_range: tuple[str | int | float, str | int | float] | None,
) -> graphs.MinimalRange | None:
    if legacy_range is None:
        return None
    legacy_lower, legacy_upper = legacy_range
    return graphs.MinimalRange(
        lower=(
            legacy_lower
            if isinstance(legacy_lower, (int | float))
            else _parse_expression(unit_parser, legacy_lower, "")
        ),
        upper=(
            legacy_upper
            if isinstance(legacy_upper, (int | float))
            else _parse_expression(unit_parser, legacy_upper, "")
        ),
    )


def _parse_legacy_graph_info(
    unit_parser: UnitParser,
    name: str,
    info: RawGraphTemplate,
) -> tuple[graphs.Graph | None, graphs.Graph | None]:
    scalars = list(_parse_legacy_scalars(unit_parser, info.get("scalars", [])))
    lower_scalars, upper_scalars = _parse_lower_or_upper_scalars(scalars)
    minimal_range = _parse_legacy_range(unit_parser, info.get("range"))
    (
        lower_compound_lines,
        lower_simple_lines,
        upper_compound_lines,
        upper_simple_lines,
    ) = _parse_legacy_metrics(unit_parser, info["metrics"])
    optional_metrics = info.get("optional_metrics", [])
    conflicting_metrics = info.get("conflicting_metrics", [])

    lower: graphs.Graph | None = None
    if lower_compound_lines or lower_simple_lines:
        lower = graphs.Graph(
            name=name,
            title=Title(str(info["title"])),
            minimal_range=minimal_range,
            compound_lines=lower_compound_lines,
            simple_lines=list(lower_simple_lines) + list(lower_scalars),
            optional=optional_metrics,
            conflicting=conflicting_metrics,
        )

    upper: graphs.Graph | None = None
    if upper_compound_lines or upper_simple_lines:
        simple_lines = list(upper_simple_lines)
        if upper_scalars:
            simple_lines.extend(upper_scalars)
        else:
            _LOGGER.info("Check scalars manually: %r, %r", name, scalars)
            simple_lines.extend(scalars)
        upper = graphs.Graph(
            name=name,
            title=Title(str(info["title"])),
            minimal_range=minimal_range,
            compound_lines=upper_compound_lines,
            simple_lines=simple_lines,
            optional=optional_metrics,
            conflicting=conflicting_metrics,
        )

    return lower, upper


def _parse_legacy_graph_infos(
    debug: bool,
    unparseables: list[Unparseable],
    unit_parser: UnitParser,
    graph_info: Mapping[str, RawGraphTemplate],
) -> Iterator[graphs.Graph | graphs.Bidirectional]:
    for name, info in graph_info.items():
        try:
            lower, upper = _parse_legacy_graph_info(unit_parser, name, info)
        except Exception as e:
            _show_exception(e)
            if debug:
                raise e
            unparseables.append(Unparseable("graphs", name))
            continue

        if lower is not None and upper is not None:
            yield graphs.Bidirectional(
                name=f"{lower.name}_{upper.name}",
                title=Title(str(info["title"])),
                lower=lower,
                upper=upper,
            )
        elif lower is not None and upper is None:
            yield lower
        elif lower is None and upper is not None:
            yield upper


@dataclass(frozen=True)
class MigratedObjects:
    metrics: Sequence[metrics.Metric]
    translations: Sequence[translations.Translation]
    perfometers: Sequence[perfometers.Perfometer | perfometers.Bidirectional | perfometers.Stacked]
    graph_templates: Sequence[graphs.Graph | graphs.Bidirectional]

    def __iter__(self):
        yield from self.metrics
        yield from self.translations
        yield from self.perfometers
        yield from self.graph_templates


def _migrate(
    debug: bool,
    unparseables: list[Unparseable],
    unit_parser: UnitParser,
    legacy_metric_info: Mapping[str, MetricInfo],
    legacy_check_metrics: Mapping[str, Mapping[MetricName, CheckMetricEntry]],
    legacy_perfometer_info: Sequence[PerfometerSpec],
    legacy_graph_info: Mapping[str, RawGraphTemplate],
) -> MigratedObjects:
    return MigratedObjects(
        list(_parse_legacy_metric_infos(debug, unparseables, unit_parser, legacy_metric_info)),
        list(_parse_legacy_check_metrics(debug, unparseables, legacy_check_metrics)),
        list(
            _parse_legacy_perfometer_infos(debug, unparseables, unit_parser, legacy_perfometer_info)
        ),
        list(_parse_legacy_graph_infos(debug, unparseables, unit_parser, legacy_graph_info)),
    )


# .
#   .--sanitize------------------------------------------------------------.
#   |                                   _ _   _                            |
#   |                   ___  __ _ _ __ (_) |_(_)_______                    |
#   |                  / __|/ _` | '_ \| | __| |_  / _ \                   |
#   |                  \__ \ (_| | | | | | |_| |/ /  __/                   |
#   |                  |___/\__,_|_| |_|_|\__|_/___\___|                   |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def _collect_metric_names_from_quantity(
    quantity: (
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ),
) -> Iterator[str]:
    match quantity:
        case str():
            yield quantity
        case metrics.WarningOf() | metrics.CriticalOf() | metrics.MinimumOf() | metrics.MaximumOf():
            yield quantity.metric_name
        case metrics.Sum():
            for summand in quantity.summands:
                yield from _collect_metric_names_from_quantity(summand)
        case metrics.Product():
            for factor in quantity.factors:
                yield from _collect_metric_names_from_quantity(factor)
        case metrics.Difference():
            yield from _collect_metric_names_from_quantity(quantity.minuend)
            yield from _collect_metric_names_from_quantity(quantity.subtrahend)
        case metrics.Fraction():
            yield from _collect_metric_names_from_quantity(quantity.dividend)
            yield from _collect_metric_names_from_quantity(quantity.divisor)


def _collect_metric_names_from_graph(migrated_graph_template: graphs.Graph) -> Iterator[str]:
    for compound_line in migrated_graph_template.compound_lines:
        yield from _collect_metric_names_from_quantity(compound_line)
    for simple_line in migrated_graph_template.simple_lines:
        yield from _collect_metric_names_from_quantity(simple_line)


def _sanitize_metric_colors(
    migrated_metrics: Sequence[metrics.Metric],
    migrated_graph_templates: Sequence[graphs.Graph | graphs.Bidirectional],
) -> Sequence[metrics.Metric]:
    colors_by_metric_name: dict[str, metrics.Color] = {}
    for migrated_graph_template in migrated_graph_templates:
        if isinstance(migrated_graph_template, graphs.Bidirectional):
            if (
                len(
                    lower_metric_names := set(
                        _collect_metric_names_from_graph(migrated_graph_template.lower)
                    )
                )
                == 1
            ) and (
                len(
                    upper_metric_names := set(
                        _collect_metric_names_from_graph(migrated_graph_template.upper)
                    )
                )
                == 1
            ):
                colors_by_metric_name[lower_metric_names.pop()] = metrics.Color.BLUE
                colors_by_metric_name[upper_metric_names.pop()] = metrics.Color.GREEN

    sanitized_metrics: list[metrics.Metric] = []
    for migrated_metric in migrated_metrics:
        if migrated_metric.name in colors_by_metric_name:
            sanitized_metrics.append(
                metrics.Metric(
                    name=migrated_metric.name,
                    title=migrated_metric.title,
                    unit=migrated_metric.unit,
                    color=colors_by_metric_name[migrated_metric.name],
                )
            )
        else:
            sanitized_metrics.append(migrated_metric)
    return sanitized_metrics


def _sanitize(migrated_objects: MigratedObjects) -> MigratedObjects:
    return MigratedObjects(
        _sanitize_metric_colors(migrated_objects.metrics, migrated_objects.graph_templates),
        migrated_objects.translations,
        migrated_objects.perfometers,
        migrated_objects.graph_templates,
    )


#  .
#   .--repr----------------------------------------------------------------.
#   |                                                                      |
#   |                         _ __ ___ _ __  _ __                          |
#   |                        | '__/ _ \ '_ \| '__|                         |
#   |                        | | |  __/ |_) | |                            |
#   |                        |_|  \___| .__/|_|                            |
#   |                                 |_|                                  |
#   '----------------------------------------------------------------------'


def _list_repr(l: Sequence[str]) -> str:
    trailing_comma = "," if len(l) > 1 else ""
    return f"[{', '.join(l)}{trailing_comma}]"


def _kwarg_repr(k: str, v: str) -> str:
    return f"{k}={v}"


def _dict_repr(d: Mapping[str, str]) -> str:
    d_ = [f"{k}: {v}" for k, v in d.items()]
    trailing_comma = "," if len(d) > 1 else ""
    return f"{{{', '.join(d_)}{trailing_comma}}}"


def _name_repr(name: str) -> str:
    return f"{name!r}"


def _title_repr(title: Title) -> str:
    return f'Title("{str(title.localize(lambda v: v))}")'


def _notation_repr(
    notation: (
        metrics.DecimalNotation
        | metrics.SINotation
        | metrics.IECNotation
        | metrics.StandardScientificNotation
        | metrics.EngineeringScientificNotation
        | metrics.TimeNotation
    ),
) -> str:
    if isinstance(notation, metrics.TimeNotation):
        return f"metrics.{notation.__class__.__name__}()"
    return f"metrics.{notation.__class__.__name__}({notation.symbol!r})"


def _precision_repr(precision: metrics.AutoPrecision | metrics.StrictPrecision) -> str:
    return f"metrics.{precision.__class__.__name__}({precision.digits})"


def _unit_repr(unit: metrics.Unit) -> str:
    if unit.precision == metrics.AutoPrecision(2):
        return f"metrics.Unit({_notation_repr(unit.notation)})"
    return f"metrics.Unit({_notation_repr(unit.notation)}, {_precision_repr(unit.precision)})"


def _color_repr(color: metrics.Color) -> str:
    return f"metrics.Color.{color.name}"


def _inst_repr(
    namespace: Literal["metrics", "translations", "perfometers", "graphs"],
    inst: object,
    args: Sequence[str],
) -> str:
    trailing_comma = "," if len(args) > 1 else ""
    return f"{namespace}.{inst.__class__.__name__}({', '.join(args)}{trailing_comma})"


def _metric_repr(unit_parser: UnitParser, metric: metrics.Metric) -> str:
    return _inst_repr(
        "metrics",
        metric,
        [
            _kwarg_repr("name", _name_repr(metric.name)),
            _kwarg_repr("title", _title_repr(metric.title)),
            _kwarg_repr("unit", unit_parser.find_unit_name(metric.unit)),
            _kwarg_repr("color", _color_repr(metric.color)),
        ],
    )


def _quantity_repr(
    unit_parser: UnitParser,
    quantity: (
        str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ),
) -> str:
    match quantity:
        case str():
            return _name_repr(quantity)
        case metrics.Constant():
            args = [
                _title_repr(quantity.title),
                unit_parser.find_unit_name(quantity.unit),
                _color_repr(quantity.color),
                str(quantity.value),
            ]
        case metrics.WarningOf():
            args = [
                _name_repr(quantity.metric_name),
            ]
        case metrics.CriticalOf():
            args = [
                _name_repr(quantity.metric_name),
            ]
        case metrics.MinimumOf():
            args = [
                _name_repr(quantity.metric_name),
                _color_repr(quantity.color),
            ]
        case metrics.MaximumOf():
            args = [
                _name_repr(quantity.metric_name),
                _color_repr(quantity.color),
            ]
        case metrics.Sum():
            args = [
                _title_repr(quantity.title),
                _color_repr(quantity.color),
                _list_repr([_quantity_repr(unit_parser, f) for f in quantity.summands]),
            ]
        case metrics.Product():
            args = [
                _title_repr(quantity.title),
                unit_parser.find_unit_name(quantity.unit),
                _color_repr(quantity.color),
                _list_repr([_quantity_repr(unit_parser, f) for f in quantity.factors]),
            ]
        case metrics.Difference():
            args = [
                _title_repr(quantity.title),
                _color_repr(quantity.color),
                _kwarg_repr("minuend", _quantity_repr(unit_parser, quantity.minuend)),
                _kwarg_repr("subtrahend", _quantity_repr(unit_parser, quantity.subtrahend)),
            ]
        case metrics.Fraction():
            args = [
                _title_repr(quantity.title),
                unit_parser.find_unit_name(quantity.unit),
                _color_repr(quantity.color),
                _kwarg_repr("dividend", _quantity_repr(unit_parser, quantity.dividend)),
                _kwarg_repr("divisor", _quantity_repr(unit_parser, quantity.divisor)),
            ]
    return _inst_repr("metrics", quantity, args)


def _check_command_repr(
    check_command: (
        translations.PassiveCheck
        | translations.ActiveCheck
        | translations.HostCheckCommand
        | translations.NagiosPlugin
    ),
) -> str:
    return _inst_repr(
        "translations",
        check_command,
        [
            _name_repr(check_command.name),
        ],
    )


def _translation_ty_repr(
    translation_ty: translations.RenameTo | translations.ScaleBy | translations.RenameToAndScaleBy,
) -> str:
    match translation_ty:
        case translations.RenameTo():
            args = [_name_repr(translation_ty.metric_name)]
        case translations.ScaleBy():
            args = [str(translation_ty.factor)]
        case translations.RenameToAndScaleBy():
            args = [
                _name_repr(translation_ty.metric_name),
                str(translation_ty.factor),
            ]
    return _inst_repr("translations", translation_ty, args)


def translation_repr(translation_: translations.Translation) -> str:
    return _inst_repr(
        "translations",
        translation_,
        [
            _kwarg_repr("name", _name_repr(translation_.name)),
            _kwarg_repr(
                "check_commands",
                _list_repr([_check_command_repr(c) for c in translation_.check_commands]),
            ),
            _kwarg_repr(
                "translations",
                _dict_repr(
                    {
                        _name_repr(n): _translation_ty_repr(t)
                        for n, t in translation_.translations.items()
                    }
                ),
            ),
        ],
    )


def _bound_value_repr(
    unit_parser: UnitParser,
    bound_value: (
        int
        | float
        | str
        | metrics.Constant
        | metrics.WarningOf
        | metrics.CriticalOf
        | metrics.MinimumOf
        | metrics.MaximumOf
        | metrics.Sum
        | metrics.Product
        | metrics.Difference
        | metrics.Fraction
    ),
) -> str:
    if isinstance(bound_value, (int, float)):
        return str(bound_value)
    return _quantity_repr(unit_parser, bound_value)


def _bound_repr(unit_parser: UnitParser, bound: perfometers.Closed | perfometers.Open) -> str:
    return _inst_repr(
        "perfometers",
        bound,
        [
            _bound_value_repr(unit_parser, bound.value),
        ],
    )


def _focus_range_repr(unit_parser: UnitParser, focus_range: perfometers.FocusRange) -> str:
    return _inst_repr(
        "perfometers",
        focus_range,
        [
            _bound_repr(unit_parser, focus_range.lower),
            _bound_repr(unit_parser, focus_range.upper),
        ],
    )


def _perfometer_repr(unit_parser: UnitParser, perfometer: perfometers.Perfometer) -> str:
    return _inst_repr(
        "perfometers",
        perfometer,
        [
            _kwarg_repr("name", _name_repr(perfometer.name)),
            _kwarg_repr("focus_range", _focus_range_repr(unit_parser, perfometer.focus_range)),
            _kwarg_repr(
                "segments",
                _list_repr([_quantity_repr(unit_parser, s) for s in perfometer.segments]),
            ),
        ],
    )


def _p_bidirectional_repr(unit_parser: UnitParser, perfometer: perfometers.Bidirectional) -> str:
    return _inst_repr(
        "perfometers",
        perfometer,
        [
            _kwarg_repr("name", _name_repr(perfometer.name)),
            _kwarg_repr("left", _perfometer_repr(unit_parser, perfometer.left)),
            _kwarg_repr("right", _perfometer_repr(unit_parser, perfometer.right)),
        ],
    )


def _p_stacked_repr(unit_parser: UnitParser, perfometer: perfometers.Stacked) -> str:
    return _inst_repr(
        "perfometers",
        perfometer,
        [
            _kwarg_repr("name", _name_repr(perfometer.name)),
            _kwarg_repr("lower", _perfometer_repr(unit_parser, perfometer.lower)),
            _kwarg_repr("upper", _perfometer_repr(unit_parser, perfometer.upper)),
        ],
    )


def perfometer_repr(
    unit_parser: UnitParser,
    perfometer: perfometers.Perfometer | perfometers.Bidirectional | perfometers.Stacked,
) -> str:
    match perfometer:
        case perfometers.Perfometer():
            return _perfometer_repr(unit_parser, perfometer)
        case perfometers.Bidirectional():
            return _p_bidirectional_repr(unit_parser, perfometer)
        case perfometers.Stacked():
            return _p_stacked_repr(unit_parser, perfometer)


def _minimal_range_repr(unit_parser: UnitParser, minimal_range: graphs.MinimalRange) -> str:
    return _inst_repr(
        "graphs",
        minimal_range,
        [
            _bound_value_repr(unit_parser, minimal_range.lower),
            _bound_value_repr(unit_parser, minimal_range.upper),
        ],
    )


def _g_graph_repr(unit_parser: UnitParser, graph: graphs.Graph) -> str:
    args = [
        _kwarg_repr("name", _name_repr(graph.name)),
        _kwarg_repr("title", _title_repr(graph.title)),
    ]
    if graph.minimal_range:
        args.append(
            _kwarg_repr("minimal_range", _minimal_range_repr(unit_parser, graph.minimal_range))
        )
    if graph.compound_lines:
        args.append(
            _kwarg_repr(
                "compound_lines",
                _list_repr([_quantity_repr(unit_parser, l) for l in graph.compound_lines]),
            )
        )
    if graph.simple_lines:
        args.append(
            _kwarg_repr(
                "simple_lines",
                _list_repr([_quantity_repr(unit_parser, l) for l in graph.simple_lines]),
            )
        )
    if graph.optional:
        args.append(_kwarg_repr("optional", _list_repr([_name_repr(o) for o in graph.optional])))
    if graph.conflicting:
        args.append(
            _kwarg_repr("conflicting", _list_repr([_name_repr(o) for o in graph.conflicting]))
        )
    return _inst_repr("graphs", graph, args)


def _g_bidirectional_repr(unit_parser: UnitParser, graph: graphs.Bidirectional) -> str:
    return _inst_repr(
        "graphs",
        graph,
        [
            _kwarg_repr("name", _name_repr(graph.name)),
            _kwarg_repr("title", _title_repr(graph.title)),
            _kwarg_repr("lower", _g_graph_repr(unit_parser, graph.lower)),
            _kwarg_repr("upper", _g_graph_repr(unit_parser, graph.upper)),
        ],
    )


def _graph_repr(unit_parser: UnitParser, graph: graphs.Graph | graphs.Bidirectional) -> str:
    match graph:
        case graphs.Graph():
            return _g_graph_repr(unit_parser, graph)
        case graphs.Bidirectional():
            return _g_bidirectional_repr(unit_parser, graph)


def _obj_repr(
    unit_parser: UnitParser,
    obj: (
        metrics.Metric
        | translations.Translation
        | perfometers.Perfometer
        | perfometers.Bidirectional
        | perfometers.Stacked
        | graphs.Graph
        | graphs.Bidirectional
    ),
) -> str:
    def _obj_var_name() -> str:
        return obj.name.replace(".", "_").replace("-", "_")

    match obj:
        case metrics.Metric():
            return f"metric_{_obj_var_name()} = {_metric_repr(unit_parser, obj)}"
        case translations.Translation():
            return f"translation_{_obj_var_name()} = {translation_repr(obj)}"
        case perfometers.Perfometer() | perfometers.Bidirectional() | perfometers.Stacked():
            return f"perfometer_{_obj_var_name()} = {perfometer_repr(unit_parser, obj)}"
        case graphs.Graph() | graphs.Bidirectional():
            return f"graph_{_obj_var_name()} = {_graph_repr(unit_parser, obj)}"


# .


def main() -> None:
    args = _parse_arguments()
    _setup_logger(args.debug)

    (
        legacy_metric_info,
        legacy_check_metrics,
        legacy_perfometer_info,
        legacy_graph_info,
    ) = _load(args.debug, args.folders, args.translations)

    all_connected_objects = _compute_connected_objects(
        args.debug,
        args.folders,
        args.metric_names,
        legacy_metric_info,
        legacy_perfometer_info,
        legacy_graph_info,
    )

    if args.check:
        for connected_objects in all_connected_objects:
            print(connected_objects)
        return

    unparseables: list[Unparseable] = []
    unit_parser = UnitParser()
    migrated_objects = _migrate(
        args.debug,
        unparseables,
        unit_parser,
        {n: m for c in all_connected_objects for n, m in c.metrics.items()},
        legacy_check_metrics,
        [p.spec for c in all_connected_objects for p in c.perfometers],
        {g.ident: g.template for c in all_connected_objects for g in c.graph_templates},
    )

    if args.sanitize:
        migrated_objects = _sanitize(migrated_objects)

    if migrated_objects:
        print("\n".join([f"{u.name} = {_unit_repr(u.unit)}" for u in unit_parser.units]))
    for migrated_object in migrated_objects:
        print(_obj_repr(unit_parser, migrated_object))
    for unparseable in unparseables:
        _LOGGER.info("%s", unparseable)


if __name__ == "__main__":
    main()
