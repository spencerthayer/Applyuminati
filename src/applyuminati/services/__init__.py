"""Application services.

The single implementation of every user-facing operation. The FastAPI routers
and the Typer CLI are both thin adapters over this package — neither contains
business logic, which is what keeps "the CLI does something subtly different
from the UI" from ever becoming true.

Services take a :class:`Repositories` bundle (one transaction) plus settings,
and return domain objects or the small read models in
:mod:`applyuminati.services.views`. They never return wire DTOs; that mapping
belongs to the API layer.
"""

from applyuminati.services.application_service import ApplicationService
from applyuminati.services.container import (
    Repositories,
    ServiceContainer,
    get_container,
    set_container,
)
from applyuminati.services.dashboard_service import DashboardService
from applyuminati.services.discovery_service import DiscoveryService
from applyuminati.services.health_service import HealthService
from applyuminati.services.job_service import JobService
from applyuminati.services.profile_service import ProfileService
from applyuminati.services.scoring_service import ScoringService
from applyuminati.services.settings_service import SettingsService
from applyuminati.services.source_service import SourceService

__all__ = [
    "ApplicationService",
    "DashboardService",
    "DiscoveryService",
    "HealthService",
    "JobService",
    "ProfileService",
    "Repositories",
    "ScoringService",
    "ServiceContainer",
    "SettingsService",
    "SourceService",
    "get_container",
    "set_container",
]
