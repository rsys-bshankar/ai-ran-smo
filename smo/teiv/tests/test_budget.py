"""Tests for teiv/loadtest/budget.py (plan task S3.23): docker's memory sizes read as MiB, the peaks and the peak of the sum against the 3 GiB budget, and
the Markdown the CI job summary shows. The load itself needs the running compose profile and is the CI workflow "TE&IV load"."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("budget", Path(__file__).resolve().parents[1] / "loadtest" / "budget.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


@pytest.mark.parametrize("text, mib", [("1.05GiB", 1075.2), ("527.3MiB", 527.3), ("512KiB", 0.5), ("2GB", 1907.3), (" 0B ", 0.0)])
def test_docker_sizes_are_read_as_mib(text, mib):
    """`docker stats` writes binary and decimal units; both become MiB."""
    assert budget.mib(text) == pytest.approx(mib, abs=0.1)


def test_a_size_that_is_not_one_is_refused():
    """An unknown unit or no number is a ValueError, never a zero."""
    for text in ("--", "12 parsecs", ""):
        with pytest.raises(ValueError):
            budget.mib(text)


def test_the_budget_holds_the_peak_of_the_sum_not_the_sum_of_the_peaks():
    """Two containers peaking at different moments: the sum of their peaks is over the budget, the peak of their sum is not, and that is what counts."""
    readings = [{"teiv-ingestion": 1500, "teiv-exposure": 400, "teiv-db": 300, "kafka": 500},
                {"teiv-ingestion": 900, "teiv-exposure": 400, "teiv-db": 300, "kafka": 1400}]
    s = budget.summarise(readings, 3072)
    assert s["peakMiB"]["teiv-ingestion"] == 1500 and s["peakMiB"]["kafka"] == 1400
    assert s["peakTotalMiB"] == 3000 and s["within"] is True
    assert budget.summarise(readings, 2999)["within"] is False
    assert budget.summarise([], 3072)["within"] is False


def test_the_markdown_names_every_container_the_sum_and_the_verdict():
    """The job summary: a row per container, the peak of the sum, within or OVER."""
    readings = [dict.fromkeys(budget.SERVICES, 100.0)]
    result = {"taken": "t", "machine": {"platform": "p", "cpus": 4, "memory": "16 GB"}, "reads": 10, "readSeconds": 60,
              "load": {"cells": 1000, "mois": 18004, "entities": 18004, "relationships": 44003, "events": 61, "seconds": 50.0},
              "memory": budget.summarise(readings, 3072)}
    text = budget.markdown(result)
    assert all(f"| {s} |" in text for s in budget.SERVICES)
    assert "**400.0**" in text and "**within**" in text
