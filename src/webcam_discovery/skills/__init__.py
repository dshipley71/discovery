"""Webcam discovery skills library — exports all skill classes and I/O models."""

from webcam_discovery.skills.traversal import (
    DirectoryTraversalSkill,
    FeedExtractionSkill,
    TraversalInput,
    TraversalOutput,
    FeedExtractionInput,
    FeedExtractionOutput,
)

from webcam_discovery.skills.validation import (
    FeedValidationSkill,
    RobotsPolicySkill,
    FeedTypeClassificationSkill,
    ValidationResult,
    RobotsPolicyInput,
    RobotsPolicyResult,
    FeedTypeInput,
    FeedTypeResult,
)

from webcam_discovery.skills.search import (
    QueryGenerationSkill,
    LocaleNavigationSkill,
    SourceDiscoverySkill,
    QueryGenerationInput,
    QueryGenerationOutput,
    LocaleNavigationInput,
    LocaleNavigationOutput,
    SourceDiscoveryInput,
    SourceDiscoveryOutput,
)

from webcam_discovery.skills.catalog import (
    DeduplicationSkill,
    GeoEnrichmentSkill,
    GeoJSONExportSkill,
    DeduplicationInput,
    DeduplicationOutput,
    GeoEnrichmentInput,
    GeoEnrichmentOutput,
    GeoJSONExportInput,
    GeoJSONExportOutput,
    CONTINENT_MAP,
)

from webcam_discovery.skills.maintenance import (
    HealthCheckSkill,
    HealthCheckInput,
    HealthCheckResult,
    HealthCheckSummary,
)

from webcam_discovery.skills.map_rendering import (
    MapRenderingSkill,
    MapRenderingInput,
    MapRenderingOutput,
)

from webcam_discovery.skills.browser_validation import (
    BrowserValidationSkill,
    BrowserValidationInput,
    BrowserValidationOutput,
)

from webcam_discovery.skills.ffprobe_validation import (
    FfprobeValidationSkill,
    FfprobeResult,
)

__all__ = [
    # traversal
    "DirectoryTraversalSkill",
    "FeedExtractionSkill",
    "TraversalInput",
    "TraversalOutput",
    "FeedExtractionInput",
    "FeedExtractionOutput",
    # validation
    "FeedValidationSkill",
    "RobotsPolicySkill",
    "FeedTypeClassificationSkill",
    "ValidationResult",
    "RobotsPolicyInput",
    "RobotsPolicyResult",
    "FeedTypeInput",
    "FeedTypeResult",
    # search
    "QueryGenerationSkill",
    "LocaleNavigationSkill",
    "SourceDiscoverySkill",
    "QueryGenerationInput",
    "QueryGenerationOutput",
    "LocaleNavigationInput",
    "LocaleNavigationOutput",
    "SourceDiscoveryInput",
    "SourceDiscoveryOutput",
    # catalog
    "DeduplicationSkill",
    "GeoEnrichmentSkill",
    "GeoJSONExportSkill",
    "DeduplicationInput",
    "DeduplicationOutput",
    "GeoEnrichmentInput",
    "GeoEnrichmentOutput",
    "GeoJSONExportInput",
    "GeoJSONExportOutput",
    "CONTINENT_MAP",
    # maintenance
    "HealthCheckSkill",
    "HealthCheckInput",
    "HealthCheckResult",
    "HealthCheckSummary",
    # map_rendering
    "MapRenderingSkill",
    "MapRenderingInput",
    "MapRenderingOutput",
    # browser_validation
    "BrowserValidationSkill",
    "BrowserValidationInput",
    "BrowserValidationOutput",
    # ffprobe_validation
    "FfprobeValidationSkill",
    "FfprobeResult",
]
