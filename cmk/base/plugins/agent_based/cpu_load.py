#!/usr/bin/env python3
# Copyright (C) 2021 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.plugins.lib.cpu import Section
from cmk.plugins.lib.cpu_load import check_cpu_load

from .agent_based_api.v1 import register, Service
from .agent_based_api.v1.type_defs import DiscoveryResult


def discover_cpu_load(section: Section) -> DiscoveryResult:
    yield Service()


register.check_plugin(
    name="cpu_loads",
    service_name="CPU load",
    discovery_function=discover_cpu_load,
    check_function=check_cpu_load,
    check_default_parameters={
        "levels1": None,
        "levels5": None,
        "levels15": (5.0, 10.0),
    },
    check_ruleset_name="cpu_load",
    sections=["cpu"],
)
