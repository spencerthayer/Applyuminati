"""JSON Resume export: project the canonical profile back to the interchange format.

Round-trips with the importer: ``export(import(payload))`` equals ``payload``
for any schema-valid input, modulo key ordering and dropped empty collections.
"""

from __future__ import annotations

from typing import Any

from applyuminati.core.models.profile import CareerProfile

__all__ = ["export_json_resume"]


def export_json_resume(profile: CareerProfile) -> dict[str, Any]:
    """Return a JSON Resume dict from the profile's stored resume document."""
    return profile.resume.to_json_dict()
