#!/usr/bin/env python3
"""
Comprehensive End-to-End Integration Test Suite for Markdown Web Browser
Ultra-thorough testing with extreme detail, no mocks, production-grade validation
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import httpx
import json
import os
import psutil
import re
import sys
import time
import traceback
from collections import defaultdict
# Removed unused import: from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable, Set
from urllib.parse import urlparse

import numpy as np
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
    TransferSpeedColumn,
    DownloadColumn,
)
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule
from rich import box
from rich.layout import Layout
from rich.align import Align
from rich.markdown import Markdown
from rich.traceback import Traceback
from rich.prompt import Prompt, Confirm
from rich.status import Status
from rich.pretty import Pretty

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.settings import get_settings


# === ENUMS AND DATA CLASSES ===

class TestCategory(Enum):
    """Test categories for organization."""
    SMOKE = "Smoke Tests"
    FUNCTIONAL = "Functional Tests"
    PERFORMANCE = "Performance Tests"
    STRESS = "Stress Tests"
    SECURITY = "Security Tests"
    EDGE_CASES = "Edge Cases"
    REGRESSION = "Regression Tests"


class TestPriority(Enum):
    """Test priority levels."""
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4


class ValidationLevel(Enum):
    """Validation thoroughness levels."""
    BASIC = "Basic validation"
    STANDARD = "Standard validation"
    THOROUGH = "Thorough validation"
    EXHAUSTIVE = "Exhaustive validation"


@dataclass
class TestMetrics:
    """Comprehensive test metrics."""
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    # Performance metrics
    api_latencies: List[float] = field(default_factory=list)
    memory_usage: List[float] = field(default_factory=list)
    cpu_usage: List[float] = field(default_factory=list)

    # Pipeline metrics
    browser_start_ms: Optional[float] = None
    capture_ms: Optional[float] = None
    tiling_ms: Optional[float] = None
    ocr_ms: Optional[float] = None
    stitching_ms: Optional[float] = None

    # Quality metrics
    tiles_generated: int = 0
    tiles_processed: int = 0
    ocr_accuracy: Optional[float] = None
    links_extracted: int = 0
    warnings_count: int = 0

    # Network metrics
    bytes_sent: int = 0
    bytes_received: int = 0
    requests_made: int = 0

    def calculate_summary(self) -> Dict[str, Any]:
        """Calculate summary statistics."""
        if not self.end_time:
            self.end_time = time.time()

        total_time = self.end_time - self.start_time

        return {
            "total_time_s": round(total_time, 2),
            "avg_api_latency_ms": round(np.mean(self.api_latencies) * 1000, 2) if self.api_latencies else 0,
            "p95_api_latency_ms": round(np.percentile(self.api_latencies, 95) * 1000, 2) if self.api_latencies else 0,
            "avg_memory_mb": round(np.mean(self.memory_usage), 2) if self.memory_usage else 0,
            "peak_memory_mb": round(max(self.memory_usage), 2) if self.memory_usage else 0,
            "avg_cpu_percent": round(np.mean(self.cpu_usage), 2) if self.cpu_usage else 0,
            "total_pipeline_ms": sum(filter(None, [
                self.browser_start_ms,
                self.capture_ms,
                self.tiling_ms,
                self.ocr_ms,
                self.stitching_ms
            ])),
            "tiles_success_rate": (self.tiles_processed / self.tiles_generated * 100) if self.tiles_generated else 0,
            "total_requests": self.requests_made,
            "total_mb_transferred": round((self.bytes_sent + self.bytes_received) / (1024 * 1024), 2),
        }


@dataclass
class ValidationResult:
    """Detailed validation result."""
    passed: bool
    category: str
    check_name: str
    expected: Any
    actual: Any
    message: str
    severity: str = "info"  # info, warning, error, critical
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return {
            "passed": self.passed,
            "category": self.category,
            "check": self.check_name,
            "expected": str(self.expected),
            "actual": str(self.actual),
            "message": self.message,
            "severity": self.severity,
            "details": self.details
        }


@dataclass
class TestCase:
    """Comprehensive test case definition."""
    id: str
    name: str
    description: str
    category: TestCategory
    priority: TestPriority
    url: str

    # Test configuration
    timeout: float = 60.0
    retries: int = 0
    validation_level: ValidationLevel = ValidationLevel.STANDARD
    profile_id: Optional[str] = None
    use_cache: bool = False

    # Expected results
    expected_tiles_min: Optional[int] = None
    expected_tiles_max: Optional[int] = None
    expected_links_min: Optional[int] = None
    expected_markdown_patterns: List[str] = field(default_factory=list)
    expected_warnings: List[str] = field(default_factory=list)
    forbidden_patterns: List[str] = field(default_factory=list)

    # Test metadata
    tags: Set[str] = field(default_factory=set)
    dependencies: List[str] = field(default_factory=list)
    cleanup: Optional[Callable] = None

    # Results storage
    metrics: TestMetrics = field(default_factory=TestMetrics)
    validations: List[ValidationResult] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def add_validation(self, result: ValidationResult):
        """Add a validation result."""
        self.validations.append(result)

    def get_status(self) -> str:
        """Get overall test status."""
        if not self.validations:
            return "pending"
        if all(v.passed for v in self.validations):
            return "passed"
        if any(v.severity == "critical" and not v.passed for v in self.validations):
            return "failed"
        if any(not v.passed for v in self.validations):
            return "partial"
        return "passed"


# === VALIDATION ENGINE ===

class ValidationEngine:
    """Comprehensive validation engine for test results."""

    def __init__(self, console: Console):
        self.console = console
        self.validators: Dict[str, Callable] = {}
        self._register_validators()

    def _register_validators(self):
        """Register all validation functions."""
        self.validators = {
            "status_code": self._validate_status_code,
            "response_time": self._validate_response_time,
            "content_type": self._validate_content_type,
            "json_schema": self._validate_json_schema,
            "markdown_structure": self._validate_markdown_structure,
            "link_extraction": self._validate_link_extraction,
            "tile_generation": self._validate_tile_generation,
            "ocr_quality": self._validate_ocr_quality,
            "warnings": self._validate_warnings,
            "artifacts": self._validate_artifacts,
            "performance": self._validate_performance,
            "security": self._validate_security,
        }

    async def validate(
        self,
        test_case: TestCase,
        response_data: Dict[str, Any],
        level: ValidationLevel = ValidationLevel.STANDARD
    ) -> List[ValidationResult]:
        """Run validations based on level."""
        results = []

        # Determine which validators to run based on level
        if level == ValidationLevel.BASIC:
            validators_to_run = ["status_code", "response_time"]
        elif level == ValidationLevel.STANDARD:
            validators_to_run = [
                "status_code", "response_time", "content_type",
                "markdown_structure", "link_extraction", "tile_generation"
            ]
        elif level == ValidationLevel.THOROUGH:
            validators_to_run = [
                "status_code", "response_time", "content_type", "json_schema",
                "markdown_structure", "link_extraction", "tile_generation",
                "ocr_quality", "warnings", "artifacts"
            ]
        else:  # EXHAUSTIVE
            validators_to_run = list(self.validators.keys())

        # Run validators
        for validator_name in validators_to_run:
            if validator_name in self.validators:
                try:
                    validator_results = await self.validators[validator_name](test_case, response_data)
                    if isinstance(validator_results, list):
                        results.extend(validator_results)
                    else:
                        results.append(validator_results)
                except Exception as e:
                    results.append(ValidationResult(
                        passed=False,
                        category="validation_error",
                        check_name=validator_name,
                        expected="No error",
                        actual=str(e),
                        message=f"Validator {validator_name} failed: {str(e)}",
                        severity="error"
                    ))

        return results

    async def _validate_status_code(self, test_case: TestCase, data: Dict[str, Any]) -> ValidationResult:
        """Validate HTTP status codes."""
        status = data.get("status_code", 0)
        expected = data.get("expected_status", 200)

        return ValidationResult(
            passed=status == expected,
            category="http",
            check_name="status_code",
            expected=expected,
            actual=status,
            message=f"HTTP status {'matches' if status == expected else 'mismatch'}",
            severity="critical" if status >= 500 else "error" if status >= 400 else "info"
        )

    async def _validate_response_time(self, test_case: TestCase, data: Dict[str, Any]) -> ValidationResult:
        """Validate response times."""
        response_time = data.get("response_time_ms", 0)
        threshold = 5000  # 5 seconds

        return ValidationResult(
            passed=response_time < threshold,
            category="performance",
            check_name="response_time",
            expected=f"< {threshold}ms",
            actual=f"{response_time}ms",
            message=f"Response time {'acceptable' if response_time < threshold else 'too slow'}",
            severity="warning" if response_time > threshold else "info"
        )

    async def _validate_content_type(self, test_case: TestCase, data: Dict[str, Any]) -> ValidationResult:
        """Validate content types."""
        content_type = data.get("content_type", "")
        expected = data.get("expected_content_type", "application/json")

        return ValidationResult(
            passed=expected in content_type,
            category="http",
            check_name="content_type",
            expected=expected,
            actual=content_type,
            message=f"Content-Type {'correct' if expected in content_type else 'incorrect'}",
            severity="error" if expected not in content_type else "info"
        )

    async def _validate_json_schema(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate JSON response against expected schema."""
        results = []
        json_data = data.get("json_response", {})

        # Check for required fields in job response
        required_fields = ["id", "state", "url", "manifest"]
        for field in required_fields:
            results.append(ValidationResult(
                passed=field in json_data,
                category="schema",
                check_name=f"field_{field}",
                expected="present",
                actual="present" if field in json_data else "missing",
                message=f"Required field '{field}' {'present' if field in json_data else 'missing'}",
                severity="error" if field not in json_data else "info"
            ))

        return results

    async def _validate_markdown_structure(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate markdown output structure."""
        results = []
        markdown = data.get("markdown", "")

        if not markdown:
            return [ValidationResult(
                passed=False,
                category="content",
                check_name="markdown_exists",
                expected="non-empty",
                actual="empty",
                message="Markdown output is empty",
                severity="critical"
            )]

        # Check for expected patterns
        for pattern in test_case.expected_markdown_patterns:
            found = re.search(pattern, markdown)
            results.append(ValidationResult(
                passed=bool(found),
                category="content",
                check_name=f"pattern_{pattern[:20]}",
                expected="present",
                actual="found" if found else "missing",
                message=f"Pattern {'found' if found else 'not found'}: {pattern[:50]}",
                severity="warning" if not found else "info"
            ))

        # Check for forbidden patterns
        for pattern in test_case.forbidden_patterns:
            found = re.search(pattern, markdown)
            results.append(ValidationResult(
                passed=not bool(found),
                category="content",
                check_name=f"forbidden_{pattern[:20]}",
                expected="absent",
                actual="absent" if not found else "present",
                message=f"Forbidden pattern {'not found (good)' if not found else 'FOUND (bad)'}: {pattern[:50]}",
                severity="error" if found else "info"
            ))

        # Structure checks
        has_headers = bool(re.search(r'^#+\s+', markdown, re.MULTILINE))
        has_links = bool(re.search(r'\[.*?\]\(.*?\)', markdown))
        has_provenance = bool(re.search(r'<!--.*tile.*-->', markdown, re.IGNORECASE))

        results.extend([
            ValidationResult(
                passed=has_headers,
                category="structure",
                check_name="has_headers",
                expected=True,
                actual=has_headers,
                message=f"Markdown {'contains' if has_headers else 'missing'} headers",
                severity="warning" if not has_headers else "info"
            ),
            ValidationResult(
                passed=has_links or not test_case.expected_links_min,
                category="structure",
                check_name="has_links",
                expected=True,
                actual=has_links,
                message=f"Markdown {'contains' if has_links else 'missing'} links",
                severity="info"
            ),
            ValidationResult(
                passed=has_provenance,
                category="structure",
                check_name="has_provenance",
                expected=True,
                actual=has_provenance,
                message=f"Provenance comments {'present' if has_provenance else 'missing'}",
                severity="info"
            ),
        ])

        return results

    async def _validate_link_extraction(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate link extraction quality."""
        results = []
        links_json = data.get("links_json", {})

        if not links_json:
            return [ValidationResult(
                passed=test_case.expected_links_min is None or test_case.expected_links_min == 0,
                category="links",
                check_name="links_exist",
                expected="> 0",
                actual="0",
                message="No links data available",
                severity="warning"
            )]

        # Count total links
        total_links = 0
        domains = links_json.get("domains", [])
        for domain in domains:
            total_links += len(domain.get("links", []))

        # Validate link count
        if test_case.expected_links_min is not None:
            results.append(ValidationResult(
                passed=total_links >= test_case.expected_links_min,
                category="links",
                check_name="link_count_min",
                expected=f">= {test_case.expected_links_min}",
                actual=str(total_links),
                message=f"Found {total_links} links (expected >= {test_case.expected_links_min})",
                severity="warning" if total_links < test_case.expected_links_min else "info"
            ))

        # Check for duplicate links
        all_urls = []
        for domain in domains:
            for link in domain.get("links", []):
                all_urls.append(link.get("url"))

        unique_urls = set(all_urls)
        duplicate_count = len(all_urls) - len(unique_urls)

        results.append(ValidationResult(
            passed=duplicate_count == 0,
            category="links",
            check_name="no_duplicates",
            expected="0",
            actual=str(duplicate_count),
            message=f"{'No duplicate' if duplicate_count == 0 else f'{duplicate_count} duplicate'} links found",
            severity="info"
        ))

        # Check link validity (basic)
        invalid_links = [url for url in all_urls if url and (url.startswith("javascript:") or url.startswith("data:"))]

        results.append(ValidationResult(
            passed=len(invalid_links) == 0,
            category="links",
            check_name="link_validity",
            expected="all valid",
            actual=f"{len(invalid_links)} invalid" if invalid_links else "all valid",
            message=f"{'All links valid' if not invalid_links else f'{len(invalid_links)} invalid links found'}",
            severity="warning" if invalid_links else "info"
        ))

        return results

    async def _validate_tile_generation(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate tile generation."""
        results = []
        manifest = data.get("manifest", {})
        tiles_total = manifest.get("tiles_total", 0)

        # Check tile count
        if test_case.expected_tiles_min is not None:
            results.append(ValidationResult(
                passed=tiles_total >= test_case.expected_tiles_min,
                category="tiles",
                check_name="tile_count_min",
                expected=f">= {test_case.expected_tiles_min}",
                actual=str(tiles_total),
                message=f"Generated {tiles_total} tiles",
                severity="error" if tiles_total < test_case.expected_tiles_min else "info"
            ))

        if test_case.expected_tiles_max is not None:
            results.append(ValidationResult(
                passed=tiles_total <= test_case.expected_tiles_max,
                category="tiles",
                check_name="tile_count_max",
                expected=f"<= {test_case.expected_tiles_max}",
                actual=str(tiles_total),
                message=f"Generated {tiles_total} tiles",
                severity="warning" if tiles_total > test_case.expected_tiles_max else "info"
            ))

        # Check overlap ratios
        overlap_ratio = manifest.get("overlap_match_ratio")
        if overlap_ratio is not None:
            results.append(ValidationResult(
                passed=overlap_ratio > 0.7,
                category="tiles",
                check_name="overlap_quality",
                expected="> 0.7",
                actual=str(round(overlap_ratio, 2)),
                message=f"Tile overlap ratio: {overlap_ratio:.2f}",
                severity="warning" if overlap_ratio < 0.7 else "info"
            ))

        return results

    async def _validate_ocr_quality(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate OCR quality metrics."""
        results = []
        manifest = data.get("manifest", {})

        # Check OCR timing
        ocr_ms = manifest.get("timings", {}).get("ocr_ms")
        if ocr_ms:
            results.append(ValidationResult(
                passed=ocr_ms < 30000,  # 30 seconds
                category="ocr",
                check_name="ocr_speed",
                expected="< 30000ms",
                actual=f"{ocr_ms}ms",
                message=f"OCR processing took {ocr_ms}ms",
                severity="warning" if ocr_ms > 30000 else "info"
            ))

        # Check OCR batches
        ocr_batches = manifest.get("ocr_batches", [])
        if ocr_batches:
            total_tiles = sum(len(batch.get("tile_ids", [])) for batch in ocr_batches)
            avg_latency = np.mean([batch.get("latency_ms", 0) for batch in ocr_batches])

            results.append(ValidationResult(
                passed=True,
                category="ocr",
                check_name="ocr_batches",
                expected="processed",
                actual=f"{len(ocr_batches)} batches",
                message=f"Processed {total_tiles} tiles in {len(ocr_batches)} batches (avg {avg_latency:.0f}ms)",
                severity="info",
                details={
                    "batch_count": len(ocr_batches),
                    "total_tiles": total_tiles,
                    "avg_latency_ms": avg_latency
                }
            ))

        return results

    async def _validate_warnings(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate warnings and errors."""
        results = []
        manifest = data.get("manifest", {})
        warnings = manifest.get("warnings", [])

        # Check warning count
        warning_count = len(warnings)
        results.append(ValidationResult(
            passed=warning_count < 5,
            category="warnings",
            check_name="warning_count",
            expected="< 5",
            actual=str(warning_count),
            message=f"{'No' if warning_count == 0 else warning_count} warnings generated",
            severity="warning" if warning_count >= 5 else "info"
        ))

        # Check for specific warning types
        warning_types = defaultdict(int)
        for warning in warnings:
            warning_types[warning.get("type", "unknown")] += 1

        for wtype, count in warning_types.items():
            severity = "error" if wtype in ["error", "critical"] else "warning"
            results.append(ValidationResult(
                passed=wtype not in ["error", "critical"],
                category="warnings",
                check_name=f"warning_type_{wtype}",
                expected="0",
                actual=str(count),
                message=f"{count} {wtype} warnings",
                severity=severity
            ))

        # Check for expected warnings
        for expected_warning in test_case.expected_warnings:
            found = any(expected_warning in str(w) for w in warnings)
            results.append(ValidationResult(
                passed=found,
                category="warnings",
                check_name=f"expected_warning",
                expected=expected_warning,
                actual="found" if found else "missing",
                message=f"Expected warning {'found' if found else 'not found'}: {expected_warning[:50]}",
                severity="info"
            ))

        return results

    async def _validate_artifacts(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate all artifacts are present and valid."""
        results = []

        artifacts_to_check = [
            ("markdown", "out.md", "text/markdown"),
            ("links_json", "links.json", "application/json"),
            ("manifest", "manifest.json", "application/json"),
        ]

        for key, filename, expected_type in artifacts_to_check:
            artifact = data.get(key)

            if artifact is None:
                results.append(ValidationResult(
                    passed=False,
                    category="artifacts",
                    check_name=f"artifact_{filename}",
                    expected="present",
                    actual="missing",
                    message=f"Artifact {filename} is missing",
                    severity="error"
                ))
            else:
                # Check size
                size = len(str(artifact)) if isinstance(artifact, (dict, list)) else len(artifact)
                results.append(ValidationResult(
                    passed=size > 0,
                    category="artifacts",
                    check_name=f"artifact_{filename}_size",
                    expected="> 0",
                    actual=str(size),
                    message=f"Artifact {filename} size: {size} bytes",
                    severity="error" if size == 0 else "info"
                ))

        return results

    async def _validate_performance(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate performance metrics."""
        results = []
        manifest = data.get("manifest", {})
        timings = manifest.get("timings", {})

        # Check total time
        total_ms = timings.get("total_ms", 0)
        if total_ms:
            results.append(ValidationResult(
                passed=total_ms < test_case.timeout * 1000,
                category="performance",
                check_name="total_time",
                expected=f"< {test_case.timeout * 1000}ms",
                actual=f"{total_ms}ms",
                message=f"Total processing time: {total_ms}ms",
                severity="error" if total_ms >= test_case.timeout * 1000 else "info"
            ))

        # Check individual stage timings
        stages = ["capture_ms", "ocr_ms", "stitch_ms"]
        for stage in stages:
            stage_time = timings.get(stage)
            if stage_time:
                # Dynamic threshold based on stage
                threshold = 10000 if "capture" in stage else 20000 if "ocr" in stage else 5000
                results.append(ValidationResult(
                    passed=stage_time < threshold,
                    category="performance",
                    check_name=f"stage_{stage}",
                    expected=f"< {threshold}ms",
                    actual=f"{stage_time}ms",
                    message=f"{stage}: {stage_time}ms",
                    severity="warning" if stage_time >= threshold else "info"
                ))

        return results

    async def _validate_security(self, test_case: TestCase, data: Dict[str, Any]) -> List[ValidationResult]:
        """Validate security aspects."""
        results = []

        # Check for sensitive data exposure
        markdown = data.get("markdown", "")
        sensitive_patterns = [
            r"api[_-]?key",
            r"secret",
            r"password",
            r"token",
            r"private[_-]?key"
        ]

        for pattern in sensitive_patterns:
            found = bool(re.search(pattern, markdown, re.IGNORECASE))
            results.append(ValidationResult(
                passed=not found,
                category="security",
                check_name=f"no_{pattern}",
                expected="not exposed",
                actual="safe" if not found else "EXPOSED",
                message=f"Sensitive pattern '{pattern}' {'not found (good)' if not found else 'FOUND (security risk)'}",
                severity="critical" if found else "info"
            ))

        # Check SSL/TLS usage
        manifest = data.get("manifest", {})
        if manifest:
            url = manifest.get("url", "")
            is_https = url.startswith("https://")
            results.append(ValidationResult(
                passed=is_https or "localhost" in url,
                category="security",
                check_name="https_usage",
                expected="https",
                actual="https" if is_https else "http",
                message=f"{'Secure' if is_https else 'Insecure'} protocol used",
                severity="warning" if not is_https and "localhost" not in url else "info"
            ))

        return results


# === SYSTEM MONITOR ===

class SystemMonitor:
    """Monitor system resources during tests."""

    def __init__(self):
        self.process = psutil.Process()
        self.start_time = time.time()
        self.samples: List[Dict[str, float]] = []
        self.monitoring = False

    async def start(self):
        """Start monitoring in background."""
        self.monitoring = True
        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> Dict[str, Any]:
        """Stop monitoring and return statistics."""
        self.monitoring = False
        await asyncio.sleep(0.5)  # Let last sample complete

        if not self.samples:
            return {}

        # Calculate statistics
        cpu_values = [s["cpu"] for s in self.samples]
        memory_values = [s["memory_mb"] for s in self.samples]

        return {
            "duration_s": time.time() - self.start_time,
            "samples": len(self.samples),
            "cpu": {
                "avg": round(np.mean(cpu_values), 2),
                "max": round(max(cpu_values), 2),
                "min": round(min(cpu_values), 2),
                "std": round(np.std(cpu_values), 2),
            },
            "memory_mb": {
                "avg": round(np.mean(memory_values), 2),
                "max": round(max(memory_values), 2),
                "min": round(min(memory_values), 2),
                "std": round(np.std(memory_values), 2),
            },
            "network": self._get_network_stats(),
        }

    async def _monitor_loop(self):
        """Background monitoring loop."""
        while self.monitoring:
            try:
                sample = {
                    "timestamp": time.time(),
                    "cpu": self.process.cpu_percent(),
                    "memory_mb": self.process.memory_info().rss / (1024 * 1024),
                    "threads": self.process.num_threads(),
                }
                self.samples.append(sample)
            except Exception:
                pass  # Ignore monitoring errors

            await asyncio.sleep(0.5)

    def _get_network_stats(self) -> Dict[str, int]:
        """Get network statistics."""
        try:
            net_io = psutil.net_io_counters()
            return {
                "bytes_sent": net_io.bytes_sent,
                "bytes_recv": net_io.bytes_recv,
                "packets_sent": net_io.packets_sent,
                "packets_recv": net_io.packets_recv,
            }
        except Exception:
            return {}


# === TEST RUNNER ===

class ComprehensiveTestRunner:
    """Ultra-thorough test runner with extreme detail."""

    def __init__(
        self,
        api_url: str = "http://localhost:8000",
        interactive: bool = False,
        parallel: int = 1,
        verbose: bool = True
    ):
        self.api_url = api_url
        self.interactive = interactive
        self.parallel = parallel
        self.verbose = verbose

        self.console = Console(width=160, record=True)
        self.settings = get_settings()
        self.validation_engine = ValidationEngine(self.console)
        self.system_monitor = SystemMonitor()

        self.test_cases: List[TestCase] = []
        self.test_results: Dict[str, Any] = {}
        self.global_metrics = TestMetrics()

        # Pre-flight checks
        self.pre_flight_passed = False
        self.server_info: Dict[str, Any] = {}

    def setup_test_suite(self):
        """Setup comprehensive test suite."""
        self.test_cases = [
            # === SMOKE TESTS ===
            TestCase(
                id="smoke_001",
                name="Basic Health Check",
                description="Verify API is responding",
                category=TestCategory.SMOKE,
                priority=TestPriority.CRITICAL,
                url="health_check",
                timeout=5.0,
                validation_level=ValidationLevel.BASIC,
                tags={"quick", "essential"}
            ),
            TestCase(
                id="smoke_002",
                name="Simple Page Capture",
                description="Capture a minimal HTML page",
                category=TestCategory.SMOKE,
                priority=TestPriority.CRITICAL,
                url="https://example.com",
                timeout=30.0,
                expected_tiles_min=1,
                expected_tiles_max=2,
                expected_links_min=1,
                expected_markdown_patterns=[r"Example Domain", r"This domain"],
                tags={"quick", "essential"}
            ),

            # === FUNCTIONAL TESTS ===
            TestCase(
                id="func_001",
                name="Complex Wikipedia Page",
                description="Test with content-rich page",
                category=TestCategory.FUNCTIONAL,
                priority=TestPriority.HIGH,
                url="https://en.wikipedia.org/wiki/Markdown",
                timeout=90.0,
                expected_tiles_min=3,
                expected_links_min=20,
                expected_markdown_patterns=[r"# Markdown", r"## ", r"###"],
                validation_level=ValidationLevel.THOROUGH,
                tags={"complex", "content"}
            ),
            TestCase(
                id="func_002",
                name="GitHub Repository Page",
                description="Test SPA with dynamic content",
                category=TestCategory.FUNCTIONAL,
                priority=TestPriority.HIGH,
                url="https://github.com/anthropics/anthropic-sdk-python",
                timeout=60.0,
                expected_tiles_min=2,
                expected_links_min=10,
                validation_level=ValidationLevel.THOROUGH,
                tags={"spa", "dynamic"}
            ),
            TestCase(
                id="func_003",
                name="News Article with Images",
                description="Test media-rich content",
                category=TestCategory.FUNCTIONAL,
                priority=TestPriority.MEDIUM,
                url="https://www.bbc.com/news",
                timeout=75.0,
                expected_tiles_min=3,
                expected_links_min=30,
                validation_level=ValidationLevel.THOROUGH,
                tags={"media", "news"}
            ),

            # === EDGE CASES ===
            TestCase(
                id="edge_001",
                name="Empty Page",
                description="Test with minimal content",
                category=TestCategory.EDGE_CASES,
                priority=TestPriority.MEDIUM,
                url="https://httpstat.us/200",
                timeout=20.0,
                expected_tiles_min=1,
                expected_tiles_max=1,
                expected_links_min=0,
                validation_level=ValidationLevel.STANDARD,
                tags={"edge", "minimal"}
            ),
            TestCase(
                id="edge_002",
                name="Very Long Page",
                description="Test with extensive scrolling",
                category=TestCategory.EDGE_CASES,
                priority=TestPriority.LOW,
                url="https://en.wikipedia.org/wiki/List_of_countries_by_population_(United_Nations)",
                timeout=120.0,
                expected_tiles_min=5,
                validation_level=ValidationLevel.STANDARD,
                tags={"edge", "large"}
            ),
            TestCase(
                id="edge_003",
                name="JavaScript Heavy SPA",
                description="Test client-side rendered content",
                category=TestCategory.EDGE_CASES,
                priority=TestPriority.MEDIUM,
                url="https://react.dev/",
                timeout=60.0,
                expected_tiles_min=2,
                validation_level=ValidationLevel.STANDARD,
                tags={"spa", "javascript"}
            ),

            # === PERFORMANCE TESTS ===
            TestCase(
                id="perf_001",
                name="Response Time Test",
                description="Measure API latencies",
                category=TestCategory.PERFORMANCE,
                priority=TestPriority.MEDIUM,
                url="https://example.com",
                timeout=20.0,
                validation_level=ValidationLevel.BASIC,
                use_cache=True,  # Test cache performance
                tags={"performance", "latency"}
            ),
            TestCase(
                id="perf_002",
                name="Large Image Processing",
                description="Test with image-heavy page",
                category=TestCategory.PERFORMANCE,
                priority=TestPriority.LOW,
                url="https://unsplash.com/",
                timeout=90.0,
                validation_level=ValidationLevel.STANDARD,
                tags={"performance", "images"}
            ),

            # === SECURITY TESTS ===
            TestCase(
                id="sec_001",
                name="HTTPS Enforcement",
                description="Verify secure connections",
                category=TestCategory.SECURITY,
                priority=TestPriority.HIGH,
                url="https://badssl.com/",
                timeout=30.0,
                validation_level=ValidationLevel.THOROUGH,
                forbidden_patterns=[r"private", r"secret", r"api[_-]key"],
                tags={"security", "ssl"}
            ),

            # === REGRESSION TESTS ===
            TestCase(
                id="reg_001",
                name="Previously Failed URL",
                description="Test known problematic pages",
                category=TestCategory.REGRESSION,
                priority=TestPriority.HIGH,
                url="https://stackoverflow.com/questions",
                timeout=60.0,
                expected_tiles_min=2,
                validation_level=ValidationLevel.THOROUGH,
                tags={"regression", "historical"}
            ),
        ]

    async def run_pre_flight_checks(self) -> bool:
        """Run comprehensive pre-flight checks."""
        self.console.rule("[cyan]Pre-Flight Checks[/cyan]")

        checks = []

        # Check API connectivity
        with self.console.status("[bold green]Checking API connectivity..."):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{self.api_url}/health")
                    checks.append(("API Health", response.status_code == 200, response.status_code))

                    if response.status_code == 200:
                        self.server_info = response.json()
            except Exception as e:
                checks.append(("API Health", False, str(e)))

        # Check required endpoints
        required_endpoints = ["/jobs", "/health", "/metrics"]
        for endpoint in required_endpoints:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.head(f"{self.api_url}{endpoint}")
                    checks.append((f"Endpoint {endpoint}", response.status_code < 500, response.status_code))
            except Exception as e:
                checks.append((f"Endpoint {endpoint}", False, str(e)))

        # Check system resources
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        checks.extend([
            ("Memory Available", memory.available > 1024 * 1024 * 1024, f"{memory.available / (1024**3):.1f}GB"),
            ("Disk Space", disk.free > 5 * 1024 * 1024 * 1024, f"{disk.free / (1024**3):.1f}GB"),
            ("CPU Count", psutil.cpu_count() > 0, f"{psutil.cpu_count()} cores"),
        ])

        # Display results
        table = Table(title="Pre-Flight Check Results", box=box.ROUNDED)
        table.add_column("Check", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Details", style="dim")

        all_passed = True
        for check_name, passed, details in checks:
            status = "[green]✅ PASS[/green]" if passed else "[red]❌ FAIL[/red]"
            table.add_row(check_name, status, str(details))
            if not passed:
                all_passed = False

        self.console.print(table)

        self.pre_flight_passed = all_passed

        if not all_passed and self.interactive:
            continue_anyway = Confirm.ask("[yellow]Pre-flight checks failed. Continue anyway?[/yellow]")
            if continue_anyway:
                all_passed = True

        return all_passed

    async def run_test_case(self, test_case: TestCase) -> TestCase:
        """Run a single test case with comprehensive validation."""

        # Start metrics
        test_case.metrics = TestMetrics()

        try:
            # Step 1: Submit job
            if self.verbose:
                self.console.print(f"[cyan]Submitting job for {test_case.name}...[/cyan]")

            if test_case.url == "health_check":
                # Special case for health check
                async with httpx.AsyncClient() as client:
                    start = time.time()
                    response = await client.get(f"{self.api_url}/health")
                    elapsed = time.time() - start

                    test_case.metrics.api_latencies.append(elapsed)

                    validation = await self.validation_engine._validate_status_code(
                        test_case,
                        {"status_code": response.status_code, "expected_status": 200}
                    )
                    test_case.add_validation(validation)
            else:
                # Normal capture job
                job_id = None

                async with httpx.AsyncClient(timeout=httpx.Timeout(test_case.timeout)) as client:
                    # Submit job
                    start = time.time()
                    response = await client.post(
                        f"{self.api_url}/jobs",
                        json={
                            "url": test_case.url,
                            "profile_id": test_case.profile_id,
                            "reuse_cache": test_case.use_cache,
                        }
                    )
                    elapsed = time.time() - start
                    test_case.metrics.api_latencies.append(elapsed)
                    test_case.metrics.requests_made += 1

                    if response.status_code in [200, 202]:
                        job_data = response.json()
                        job_id = job_data["id"]
                        test_case.artifacts["job_id"] = job_id

                        # Monitor job progress
                        if self.verbose:
                            self.console.print(f"[cyan]Monitoring job {job_id[:8]}...[/cyan]")
                        final_state = await self._monitor_job(client, job_id, test_case)

                        # Get final results
                        if final_state == "DONE":
                            if self.verbose:
                                self.console.print(f"[cyan]Retrieving artifacts...[/cyan]")
                            await self._retrieve_artifacts(client, job_id, test_case)

                            # Run validations
                            if self.verbose:
                                self.console.print(f"[cyan]Running validations...[/cyan]")
                            response_data = {
                                "status_code": 200,
                                "json_response": test_case.artifacts.get("job_data", {}),
                                "manifest": test_case.artifacts.get("manifest", {}),
                                "markdown": test_case.artifacts.get("markdown", ""),
                                "links_json": test_case.artifacts.get("links", {}),
                                "response_time_ms": sum(test_case.metrics.api_latencies) * 1000,
                            }

                            validations = await self.validation_engine.validate(
                                test_case,
                                response_data,
                                test_case.validation_level
                            )

                            for validation in validations:
                                test_case.add_validation(validation)
                        else:
                            # Job did not complete successfully - extract error details
                            error_message = f"Job failed with state: {final_state}"
                            error_details = {}

                            # Try to get error information from job_data
                            job_data = test_case.artifacts.get("job_data", {})
                            if job_data:
                                if "error" in job_data:
                                    error_message += f"\nError: {job_data['error']}"
                                    error_details["backend_error"] = job_data["error"]
                                if "error_details" in job_data:
                                    error_details["details"] = job_data["error_details"]
                                if "traceback" in job_data:
                                    error_details["traceback"] = job_data["traceback"]
                                if "warnings" in job_data:
                                    error_details["warnings"] = job_data["warnings"]

                            # Try to retrieve manifest even on failure for diagnostic info
                            try:
                                await self._retrieve_artifacts(client, job_id, test_case)
                                manifest = test_case.artifacts.get("manifest", {})
                                if manifest and "error" in manifest:
                                    error_details["manifest_error"] = manifest["error"]
                            except Exception:
                                pass  # Ignore artifact retrieval errors

                            test_case.add_validation(ValidationResult(
                                passed=False,
                                category="job",
                                check_name="job_completion",
                                expected="DONE",
                                actual=final_state,
                                message=error_message,
                                severity="critical",
                                details=error_details
                            ))
                    else:
                        test_case.add_validation(ValidationResult(
                            passed=False,
                            category="http",
                            check_name="job_submission",
                            expected="200/202",
                            actual=str(response.status_code),
                            message=f"Failed to submit job: HTTP {response.status_code}",
                            severity="critical"
                        ))

        except asyncio.TimeoutError:
            test_case.add_validation(ValidationResult(
                passed=False,
                category="timeout",
                check_name="test_timeout",
                expected=f"< {test_case.timeout}s",
                actual="timeout",
                message=f"Test timed out after {test_case.timeout}s",
                severity="critical"
            ))

        except Exception as e:
            test_case.add_validation(ValidationResult(
                passed=False,
                category="error",
                check_name="unexpected_error",
                expected="no error",
                actual=str(e),
                message=f"Unexpected error: {str(e)}",
                severity="critical",
                details={"traceback": traceback.format_exc()}
            ))

        finally:
            pass  # Status no longer needed
            test_case.metrics.end_time = time.time()

        return test_case

    async def _monitor_job(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        test_case: TestCase
    ) -> str:
        """Monitor job progress with detailed metrics."""

        poll_count = 0
        max_polls = int(test_case.timeout * 2)  # 2 polls per second
        last_state = None

        while poll_count < max_polls:
            poll_count += 1
            await asyncio.sleep(0.5)

            try:
                start = time.time()
                response = await client.get(f"{self.api_url}/jobs/{job_id}")
                elapsed = time.time() - start

                test_case.metrics.api_latencies.append(elapsed)
                test_case.metrics.requests_made += 1

                if response.status_code == 200:
                    job_data = response.json()
                    state = job_data.get("state", "UNKNOWN")

                    # Update status display
                    progress = job_data.get("progress", {})
                    if progress:
                        done = progress.get("done", 0)
                        total = progress.get("total", 0)
                        if total > 0:
                            pct = (done / total) * 100
                            if self.verbose:
                                self.console.print(f"[cyan]{test_case.name}: {state} ({pct:.0f}%)[/cyan]")
                        else:
                            if self.verbose:
                                self.console.print(f"[cyan]{test_case.name}: {state}[/cyan]")
                    else:
                        if self.verbose:
                            self.console.print(f"[cyan]{test_case.name}: {state}[/cyan]")

                    # Track state transitions
                    if state != last_state:
                        if state == "BROWSER_STARTING":
                            test_case.metrics.browser_start_ms = time.time() * 1000
                        elif state == "CAPTURE_RUNNING" and last_state == "BROWSER_READY":
                            test_case.metrics.capture_ms = time.time() * 1000
                        elif state == "OCR_RUNNING":
                            test_case.metrics.ocr_ms = time.time() * 1000

                        last_state = state

                    # Check for completion
                    if state in ["DONE", "FAILED"]:
                        test_case.artifacts["job_data"] = job_data

                        # Extract metrics from manifest
                        manifest = job_data.get("manifest", {})
                        if manifest:
                            test_case.artifacts["manifest"] = manifest
                            test_case.metrics.tiles_generated = manifest.get("tiles_total", 0)
                            test_case.metrics.warnings_count = len(manifest.get("warnings", []))

                            timings = manifest.get("timings", {})
                            if timings:
                                test_case.metrics.capture_ms = timings.get("capture_ms")
                                test_case.metrics.ocr_ms = timings.get("ocr_ms")
                                test_case.metrics.stitching_ms = timings.get("stitch_ms")

                        return state

            except Exception as e:
                # Log error but continue monitoring
                if self.verbose:
                    self.console.print(f"[yellow]Warning: Job monitoring error: {e}[/yellow]")

        return "TIMEOUT"

    async def _retrieve_artifacts(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        test_case: TestCase
    ):
        """Retrieve job artifacts."""

        artifacts = [
            ("markdown", "out.md", True),
            ("links", "links.json", False),
            ("manifest", "manifest.json", False),
        ]

        for key, endpoint, is_text in artifacts:
            try:
                response = await client.get(f"{self.api_url}/jobs/{job_id}/{endpoint}")
                test_case.metrics.requests_made += 1

                if response.status_code == 200:
                    if is_text:
                        test_case.artifacts[key] = response.text
                    else:
                        test_case.artifacts[key] = response.json()

                    test_case.metrics.bytes_received += len(response.content)
            except Exception:
                pass  # Artifact retrieval failure is handled in validation

    def create_test_report(self) -> Panel:
        """Create comprehensive test report."""

        # Calculate statistics
        total_tests = len(self.test_cases)
        passed_tests = sum(1 for tc in self.test_cases if tc.get_status() == "passed")
        failed_tests = sum(1 for tc in self.test_cases if tc.get_status() == "failed")
        partial_tests = sum(1 for tc in self.test_cases if tc.get_status() == "partial")

        # Create main results table
        results_table = Table(title="Test Execution Results", box=box.DOUBLE_EDGE, expand=True)
        results_table.add_column("ID", style="dim", width=10)
        results_table.add_column("Test Name", style="cyan", width=30)
        results_table.add_column("Category", style="blue", width=15)
        results_table.add_column("Status", style="bold", width=10)
        results_table.add_column("Validations", style="yellow", width=12)
        results_table.add_column("Time", style="magenta", width=10)
        results_table.add_column("Issues", style="red", width=30)

        for test_case in self.test_cases:
            status = test_case.get_status()

            if status == "passed":
                status_display = "[green]✅ PASS[/green]"
            elif status == "failed":
                status_display = "[red]❌ FAIL[/red]"
            elif status == "partial":
                status_display = "[yellow]⚠️ PARTIAL[/yellow]"
            else:
                status_display = "[dim]⏭️ SKIPPED[/dim]"

            # Count validation results
            passed_validations = sum(1 for v in test_case.validations if v.passed)
            total_validations = len(test_case.validations)
            validation_display = f"{passed_validations}/{total_validations}"

            # Calculate execution time
            if test_case.metrics.end_time:
                exec_time = test_case.metrics.end_time - test_case.metrics.start_time
                time_display = f"{exec_time:.2f}s"
            else:
                time_display = "N/A"

            # Find critical issues
            issues = []
            for v in test_case.validations:
                if not v.passed and v.severity in ["critical", "error"]:
                    issues.append(v.check_name)
            issues_display = ", ".join(issues[:3]) if issues else "None"
            if len(issues) > 3:
                issues_display += f" (+{len(issues)-3} more)"

            results_table.add_row(
                test_case.id,
                test_case.name[:30],
                test_case.category.value,
                status_display,
                validation_display,
                time_display,
                issues_display
            )

        # Create summary statistics
        stats_table = Table.grid(padding=1, expand=True)
        stats_table.add_column(justify="right", style="cyan")
        stats_table.add_column(style="yellow")
        stats_table.add_column(justify="right", style="cyan")
        stats_table.add_column(style="yellow")

        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0

        stats_table.add_row(
            "Total Tests:", str(total_tests),
            "Success Rate:", f"{success_rate:.1f}%"
        )
        stats_table.add_row(
            "Passed:", f"[green]{passed_tests}[/green]",
            "Failed:", f"[red]{failed_tests}[/red]"
        )
        stats_table.add_row(
            "Partial:", f"[yellow]{partial_tests}[/yellow]",
            "Skipped:", str(total_tests - passed_tests - failed_tests - partial_tests)
        )

        # Create performance summary
        perf_metrics = self.global_metrics.calculate_summary()
        perf_table = Table(title="Performance Metrics", box=box.SIMPLE)
        perf_table.add_column("Metric", style="cyan")
        perf_table.add_column("Value", style="yellow")

        for key, value in perf_metrics.items():
            perf_table.add_row(key.replace("_", " ").title(), str(value))

        # Combine into final report
        report_group = Group(
            Rule("[bold magenta]Test Execution Report[/bold magenta]"),
            results_table,
            Rule(style="dim"),
            Align.center(stats_table),
            Rule(style="dim"),
            perf_table
        )

        return Panel(report_group, border_style="magenta", box=box.DOUBLE)

    async def run_all_tests(self):
        """Run all tests with comprehensive reporting."""

        # Print header
        self.console.print("""
╔═══════════════════════════════════════════════════════════════════════════════════════════════╗
║                           COMPREHENSIVE END-TO-END TEST SUITE                                ║
║                          Ultra-Thorough Production-Grade Testing                             ║
╚═══════════════════════════════════════════════════════════════════════════════════════════════╝
        """, style="bold magenta")

        # Run pre-flight checks
        if not await self.run_pre_flight_checks():
            if not self.interactive:
                self.console.print("[red]Pre-flight checks failed. Aborting.[/red]")
                return

        # Display test plan
        self.console.rule("[cyan]Test Plan[/cyan]")

        plan_table = Table(title="Tests to Execute", box=box.ROUNDED)
        plan_table.add_column("Category", style="cyan")
        plan_table.add_column("Count", style="yellow")
        plan_table.add_column("Priority", style="magenta")

        category_counts = defaultdict(int)
        for tc in self.test_cases:
            category_counts[tc.category.value] += 1

        for category, count in category_counts.items():
            plan_table.add_row(category, str(count), "Mixed")

        self.console.print(plan_table)

        if self.interactive:
            if not Confirm.ask("\n[cyan]Proceed with test execution?[/cyan]"):
                self.console.print("[yellow]Test execution cancelled.[/yellow]")
                return

        # Start system monitoring
        await self.system_monitor.start()

        # Run tests
        self.console.rule("[cyan]Test Execution[/cyan]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=self.console
        ) as progress:

            task = progress.add_task(
                "[cyan]Running tests...",
                total=len(self.test_cases)
            )

            # Group tests by priority
            priority_groups = defaultdict(list)
            for tc in self.test_cases:
                priority_groups[tc.priority].append(tc)

            # Run tests in priority order
            for priority in [TestPriority.CRITICAL, TestPriority.HIGH, TestPriority.MEDIUM, TestPriority.LOW]:
                tests = priority_groups.get(priority, [])

                for test_case in tests:
                    # Run test
                    progress.update(task, description=f"[cyan]Running: {test_case.name}[/cyan]")

                    result = await self.run_test_case(test_case)

                    # Update progress
                    progress.advance(task)

                    # Show quick result
                    status = result.get_status()
                    if status == "passed":
                        self.console.print(f"✅ [green]{result.name}[/green]")
                    elif status == "failed":
                        self.console.print(f"❌ [red]{result.name}[/red]")
                    else:
                        self.console.print(f"⚠️ [yellow]{result.name}[/yellow]")

                    # Update global metrics
                    self.global_metrics.requests_made += result.metrics.requests_made
                    self.global_metrics.bytes_sent += result.metrics.bytes_sent
                    self.global_metrics.bytes_received += result.metrics.bytes_received

        # Stop monitoring
        system_stats = await self.system_monitor.stop()

        # Generate and display report
        self.console.rule("[magenta]Final Report[/magenta]")

        report = self.create_test_report()
        self.console.print(report)

        # Display system statistics
        if system_stats:
            sys_table = Table(title="System Resource Usage", box=box.SIMPLE)
            sys_table.add_column("Resource", style="cyan")
            sys_table.add_column("Average", style="yellow")
            sys_table.add_column("Peak", style="red")

            sys_table.add_row(
                "CPU %",
                f"{system_stats['cpu']['avg']:.1f}%",
                f"{system_stats['cpu']['max']:.1f}%"
            )
            sys_table.add_row(
                "Memory (MB)",
                f"{system_stats['memory_mb']['avg']:.0f}",
                f"{system_stats['memory_mb']['max']:.0f}"
            )

            self.console.print(sys_table)

        # Save report
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # HTML report
        project_root = Path(__file__).parent.parent
        reports_dir = project_root / "test-outputs/reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        html_path = reports_dir / f"test_report_{timestamp}.html"
        html_path.write_text(self.console.export_html(clear=False), encoding="utf-8")

        # JSON report for CI/CD
        json_report = {
            "timestamp": timestamp,
            "summary": {
                "total": len(self.test_cases),
                "passed": sum(1 for tc in self.test_cases if tc.get_status() == "passed"),
                "failed": sum(1 for tc in self.test_cases if tc.get_status() == "failed"),
                "partial": sum(1 for tc in self.test_cases if tc.get_status() == "partial"),
            },
            "tests": [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "status": tc.get_status(),
                    "validations": [v.to_dict() for v in tc.validations],
                    "metrics": tc.metrics.calculate_summary()
                }
                for tc in self.test_cases
            ],
            "system_stats": system_stats,
        }

        json_path = reports_dir / f"test_report_{timestamp}.json"
        json_path.write_text(json.dumps(json_report, indent=2), encoding="utf-8")

        self.console.print(f"\n📄 Reports saved:")
        self.console.print(f"  • HTML: {html_path}")
        self.console.print(f"  • JSON: {json_path}")

        # Final verdict
        total_tests = len(self.test_cases)
        passed_tests = sum(1 for tc in self.test_cases if tc.get_status() == "passed")
        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0

        if success_rate == 100:
            verdict = "🎉 PERFECT! All tests passed!"
            style = "bold green"
        elif success_rate >= 80:
            verdict = f"✅ EXCELLENT! {success_rate:.1f}% tests passed"
            style = "bold green"
        elif success_rate >= 60:
            verdict = f"⚠️ GOOD! {success_rate:.1f}% tests passed"
            style = "bold yellow"
        else:
            verdict = f"❌ NEEDS WORK! Only {success_rate:.1f}% tests passed"
            style = "bold red"

        self.console.print(
            Panel(
                Align.center(Text(verdict, style=style)),
                border_style=style.split()[1] if " " in style else "green",
                box=box.DOUBLE
            )
        )


# === MAIN ENTRY POINT ===

async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Comprehensive End-to-End Integration Testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                          # Run all tests
  %(prog)s --url https://finviz.com                # Test a specific URL
  %(prog)s --url https://example.com --verbose     # Test URL with detailed output
  %(prog)s --interactive                           # Interactive mode with confirmations
  %(prog)s --parallel 4                            # Run 4 tests in parallel
  %(prog)s --api-url http://prod                   # Test against production
  %(prog)s --verbose                               # Extra detailed output
        """
    )

    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API base URL to test"
    )
    parser.add_argument(
        "--url",
        help="Test a specific URL (always runs; if --category/--priority specified, also runs matching tests from suite)"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive mode with prompts"
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of parallel tests"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "--category",
        choices=[c.value for c in TestCategory],
        help="Run only tests in category"
    )
    parser.add_argument(
        "--priority",
        type=int,
        choices=[1, 2, 3, 4],
        help="Run only tests with priority <= value"
    )

    args = parser.parse_args()

    # Create and configure test runner
    runner = ComprehensiveTestRunner(
        api_url=args.api_url,
        interactive=args.interactive,
        parallel=args.parallel,
        verbose=args.verbose
    )

    # Setup test suite or create custom URL test
    if args.url:
        # Create a custom test case for the provided URL
        parsed = urlparse(args.url)
        domain = parsed.netloc or "custom"

        custom_test = TestCase(
            id="custom_url_001",
            name=f"Custom URL Test: {domain}",
            description=f"Quick test of user-provided URL: {args.url}",
            category=TestCategory.FUNCTIONAL,
            priority=TestPriority.HIGH,
            url=args.url,
            timeout=90.0,
            expected_tiles_min=1,
            validation_level=ValidationLevel.THOROUGH,
            tags={"custom", "user-provided"}
        )

        # If no other filters specified, run ONLY the custom URL test
        if not args.category and not args.priority:
            runner.test_cases = [custom_test]
        else:
            # If filters specified, run custom test PLUS filtered suite
            runner.setup_test_suite()

            # Apply filters to the full suite (not including custom test yet)
            if args.category:
                runner.test_cases = [
                    tc for tc in runner.test_cases
                    if tc.category.value == args.category
                ]

            if args.priority:
                runner.test_cases = [
                    tc for tc in runner.test_cases
                    if tc.priority.value <= args.priority
                ]

            # Always add custom test at the beginning (never filter it out)
            runner.test_cases.insert(0, custom_test)
    else:
        # Normal flow: setup full test suite
        runner.setup_test_suite()

        # Filter tests if requested
        if args.category:
            runner.test_cases = [
                tc for tc in runner.test_cases
                if tc.category.value == args.category
            ]

        if args.priority:
            runner.test_cases = [
                tc for tc in runner.test_cases
                if tc.priority.value <= args.priority
            ]

    # Run tests
    await runner.run_all_tests()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Test suite interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)