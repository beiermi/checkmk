#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# pylint: disable=unused-import

import functools
import time

from cmk.base.check_api import check_levels
from cmk.base.plugins.agent_based.agent_based_api.v1 import (
    get_rate,
    get_value_store,
    IgnoreResultsError,
)

from cmk.agent_based.v2 import render, Service
from cmk.plugins.lib.azure import AZURE_AGENT_SEPARATOR as AZURE_AGENT_SEPARATOR
from cmk.plugins.lib.azure import get_service_labels_from_resource_tags
from cmk.plugins.lib.azure import iter_resource_attributes as iter_resource_attributes
from cmk.plugins.lib.azure import parse_resources as parse_resources

_AZURE_METRIC_FMT = {
    "count": lambda n: "%d" % n,
    "percent": render.percent,
    "bytes": render.bytes,
    "bytes_per_second": render.iobandwidth,
    "seconds": lambda s: "%.2f s" % s,
    "milli_seconds": lambda ms: "%d ms" % (ms * 1000),
    "milliseconds": lambda ms: "%d ms" % (ms * 1000),
}


def get_data_or_go_stale(check_function):
    """Variant of get_parsed_item_data that raises MKCounterWrapped
    if data is not found.
    """

    @functools.wraps(check_function)
    def wrapped_check_function(item, params, parsed):
        if not isinstance(parsed, dict):
            return 3, "Wrong usage of decorator: parsed is not a dict"
        if item not in parsed or not parsed[item]:
            raise IgnoreResultsError("Data not present at the moment")
        return check_function(item, params, parsed[item])

    return wrapped_check_function


def check_azure_metric(  # pylint: disable=too-many-locals
    resource, metric_key, cmk_key, display_name, levels=None, levels_lower=None, use_rate=False
):
    metric = resource.metrics.get(metric_key)
    if metric is None:
        return None

    if use_rate:
        countername = f"{resource.id}.{metric_key}"
        value = get_rate(
            get_value_store(), countername, time.time(), metric.value, raise_overflow=True
        )
        unit = "%s_rate" % metric.unit
    else:
        value = metric.value
        unit = metric.unit

    if value is None:
        return 3, "Metric %s is 'None'" % display_name, []

    # convert to SI-unit
    if unit in ("milli_seconds", "milliseconds"):
        value /= 1000.0
    elif unit == "seconds_rate":
        # we got seconds, but we computed the rate -> seconds per second:
        # how long happend something / time period = percent of the time
        # e.g. CPU time: how much percent of of the time was the CPU busy.
        value *= 100.0
        unit = "percent"

    return check_levels(
        value,
        cmk_key,
        (levels or (None, None)) + (levels_lower or (None, None)),
        infoname=display_name,
        human_readable_func=_AZURE_METRIC_FMT.get(unit, lambda x: f"{x}"),
        boundaries=(0, None),
    )


# .

#   .--Discovery-----------------------------------------------------------.
#   |              ____  _                                                 |
#   |             |  _ \(_)___  ___ _____   _____ _ __ _   _               |
#   |             | | | | / __|/ __/ _ \ \ / / _ \ '__| | | |              |
#   |             | |_| | \__ \ (_| (_) \ V /  __/ |  | |_| |              |
#   |             |____/|_|___/\___\___/ \_/ \___|_|   \__, |              |
#   |                                                  |___/               |
#   +----------------------------------------------------------------------+


def discover_azure_by_metrics(*desired_metrics):
    """Return a discovery function, that will discover if any of the metrics are found"""

    def discovery_function(parsed):
        for name, resource in parsed.items():
            metr = resource.metrics
            if set(desired_metrics) & set(metr):
                yield Service(
                    item=name, labels=get_service_labels_from_resource_tags(resource.tags)
                )

    return discovery_function


# .
