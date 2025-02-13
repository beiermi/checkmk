#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, Iterable, Self, TypeVar

from cmk.server_side_calls.v1 import Secret

CheckCommandArguments = Iterable[int | float | str | tuple[str, str, str]]


@dataclass(frozen=True, kw_only=True)
class PreprocessingResult:
    processed_rules: Sequence[tuple[str, Sequence[Mapping[str, object]]]]
    ad_hoc_secrets: Mapping[str, str]

    @classmethod
    def from_config(
        cls, rules_by_name: Sequence[tuple[str, Sequence[Mapping[str, object]]]]
    ) -> Self:
        """
        >>> PreprocessingResult.from_config(
        ...     [
        ...         (
        ...             'pure_storage_fa',
        ...             [
        ...                 {
        ...                     'api_token': ('explicit_password', ':uuid:1234', 'knubblwubbl'),
        ...                     'timeout': 5.0,
        ...                 },
        ...             ],
        ...         ),
        ...     ],
        ... )
        PreprocessingResult(processed_rules=[('pure_storage_fa', [{'api_token': Secret(...), 'timeout': 5.0}])], ad_hoc_secrets={':uuid:1234': 'knubblwubbl'})
        """
        preprocessing_results = [
            (name, [process_configuration_to_parameters(rule) for rule in rules])
            for name, rules in rules_by_name
        ]

        return cls(
            processed_rules=[
                (name, [res.value for res in prep]) for name, prep in preprocessing_results
            ],
            ad_hoc_secrets={
                k: v
                for name, prep in preprocessing_results
                for res in prep
                for k, v in res.found_secrets.items()
            },
        )


_RuleSetType_co = TypeVar("_RuleSetType_co", covariant=True)


@dataclass(frozen=True)
class ReplacementResult(Generic[_RuleSetType_co]):
    value: _RuleSetType_co
    found_secrets: Mapping[str, str]
    surrogates: Mapping[int, str]


def process_configuration_to_parameters(
    params: Mapping[str, object]
) -> ReplacementResult[Mapping[str, object]]:
    d_results = [(k, _processed_config_value(v)) for k, v in params.items()]
    return ReplacementResult(
        value={k: res.value for k, res in d_results},
        found_secrets={k: v for _, res in d_results for k, v in res.found_secrets.items()},
        surrogates={k: v for _, res in d_results for k, v in res.surrogates.items()},
    )


def _processed_config_value(params: object) -> ReplacementResult[object]:
    match params:
        case list():
            results = [_processed_config_value(v) for v in params]
            return ReplacementResult(
                value=[res.value for res in results],
                found_secrets={k: v for res in results for k, v in res.found_secrets.items()},
                surrogates={k: v for res in results for k, v in res.surrogates.items()},
            )
        case tuple():
            match params:
                case "stored_password", str(passwd_id), str():
                    return _replace_password(passwd_id, None)
                case "explicit_password", str(passwd_id), str(value):
                    return _replace_password(passwd_id, value)
            results = [_processed_config_value(v) for v in params]
            return ReplacementResult(
                value=tuple(res.value for res in results),
                found_secrets={k: v for res in results for k, v in res.found_secrets.items()},
                surrogates={k: v for res in results for k, v in res.surrogates.items()},
            )
        case dict():
            return process_configuration_to_parameters(params)
    return ReplacementResult(value=params, found_secrets={}, surrogates={})


def _replace_password(
    name: str,
    value: str | None,
) -> ReplacementResult:
    # We need some injective function.
    surrogate = id(name)
    return ReplacementResult(
        value=Secret(surrogate),
        found_secrets={} if value is None else {name: value},
        surrogates={surrogate: name},
    )
