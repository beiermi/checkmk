#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Mapping, Sequence

import pytest

from cmk.plugins.prism.server_side_calls.special_agent import generate_prism_command
from cmk.server_side_calls.v1 import HostConfig, IPv4Config, Secret

HOST_CONFIG = HostConfig(
    name="host name",
    ipv4_config=IPv4Config(address="address"),
)


@pytest.mark.parametrize(
    ["params", "expected_args"],
    [
        pytest.param(
            {"username": "", "password": Secret(12)},
            [
                "--server",
                "address",
                "--username",
                "",
                "--password",
                Secret(12).unsafe(),
            ],
            id="explicit password and no port",
        ),
        pytest.param(
            {"username": "userid", "password": Secret(23), "port": 9440},
            [
                "--server",
                "address",
                "--username",
                "userid",
                "--password",
                Secret(23).unsafe(),
                "--port",
                "9440",
            ],
            id="explicit password and port",
        ),
        pytest.param(
            {"username": "userid", "password": Secret(42), "port": 9440},
            [
                "--server",
                "address",
                "--username",
                "userid",
                "--password",
                Secret(42).unsafe(),
                "--port",
                "9440",
            ],
            id="password from store and port",
        ),
    ],
)
def test_prism_argument_parsing(params: Mapping[str, object], expected_args: Sequence[str]) -> None:
    """Tests if all required arguments are present."""
    command = list(generate_prism_command(params, HOST_CONFIG, {}))[0]
    assert command.command_arguments == expected_args
