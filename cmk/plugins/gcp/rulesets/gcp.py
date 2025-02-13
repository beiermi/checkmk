#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Callable, Iterable, Sequence
from typing import Final

from cmk.utils.version import edition, Edition

from cmk.rulesets.v1 import Help, Title
from cmk.rulesets.v1.form_specs import (
    DefaultValue,
    DictElement,
    Dictionary,
    migrate_to_password,
    MultipleChoice,
    MultipleChoiceElement,
    Password,
    String,
)
from cmk.rulesets.v1.form_specs.validators import LengthInRange
from cmk.rulesets.v1.rule_specs import SpecialAgent, Topic

RAW_GCP_SERVICES: Final = [
    MultipleChoiceElement(name="gcs", title=Title("Google Cloud Storage (GCS)")),
    MultipleChoiceElement(name="cloud_sql", title=Title("Cloud SQL")),
    MultipleChoiceElement(name="filestore", title=Title("Filestore")),
    MultipleChoiceElement(name="gce_storage", title=Title("GCE Storage")),
    MultipleChoiceElement(name="http_lb", title=Title("HTTP(S) load balancer")),
]

CCE_GCP_SERVICES: Final = [
    MultipleChoiceElement(name="cloud_run", title=Title("Cloud Run")),
    MultipleChoiceElement(name="cloud_functions", title=Title("Cloud Functions")),
    MultipleChoiceElement(name="redis", title=Title("Memorystore Redis")),
]


def get_gcp_services() -> Sequence[MultipleChoiceElement]:
    if edition() in (Edition.CME, Edition.CCE):
        return RAW_GCP_SERVICES + CCE_GCP_SERVICES

    return RAW_GCP_SERVICES


def _get_edition_specific_choices(
    valid_service_choices: Iterable[str],
) -> Callable[[object], Sequence[str]]:
    def inner(value: object) -> Sequence[str]:
        if not isinstance(value, Iterable):
            raise TypeError(value)

        # silently cut off invalid CCE only choices if we're CEE now.
        return [s for s in value if s in valid_service_choices]

    return inner


def _parameter_form_special_agents_gcp() -> Dictionary:
    valid_service_choices = {c.name for c in get_gcp_services()}
    return Dictionary(
        title=Title("Google Cloud Platform"),
        elements={
            "project": DictElement(
                parameter_form=String(
                    title=Title("Project ID"), custom_validate=(LengthInRange(min_value=1),)
                ),
                required=True,
            ),
            "credentials": DictElement(
                parameter_form=Password(
                    title=Title("JSON credentials for service account"),
                    migrate=migrate_to_password,
                ),
                required=True,
            ),
            "services": DictElement(
                parameter_form=MultipleChoice(
                    title=Title("GCP services to monitor"),
                    elements=get_gcp_services(),
                    prefill=DefaultValue(list(valid_service_choices)),
                    # this migrate cannot be removed since it not actually a migration in the sense
                    # of updated rulesets, it implements logic for edition specific choices
                    migrate=_get_edition_specific_choices(valid_service_choices),
                ),
                required=True,
            ),
            "piggyback": DictElement(
                parameter_form=Dictionary(
                    title=Title("GCP piggyback services"),
                    elements={
                        "prefix": DictElement(
                            parameter_form=String(
                                title=Title("Custom host name prefix"),
                                help_text=Help(
                                    "Prefix for GCE piggyback host names. Defaults to project ID"
                                ),
                            ),
                            required=False,
                        ),
                        "piggyback_services": DictElement(
                            parameter_form=MultipleChoice(
                                title=Title("Piggyback services to monitor"),
                                elements=[
                                    MultipleChoiceElement(
                                        name="gce", title=Title("Google Compute Engine (GCE)")
                                    )
                                ],
                                prefill=DefaultValue(["gce"]),
                            ),
                            required=True,
                        ),
                    },
                ),
                required=True,
            ),
            "cost": DictElement(
                parameter_form=Dictionary(
                    title=Title("Costs"),
                    elements={
                        "tableid": DictElement(
                            parameter_form=String(
                                title=Title("BigQuery table ID"),
                                help_text=Help(
                                    "Table ID found in the Details of the table in the SQL workspace of BigQuery"
                                ),
                            ),
                            required=True,
                        )
                    },
                ),
                required=False,
            ),
        },
    )


rule_spec_special_agents_gcp = SpecialAgent(
    topic=Topic.CLOUD,
    name="gcp",
    title=Title("Google Cloud Platform (GCP)"),
    parameter_form=_parameter_form_special_agents_gcp,
)
