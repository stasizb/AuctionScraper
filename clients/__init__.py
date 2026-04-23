"""Abstractions over external systems (bidfax.info, copart.com, iaai.com).

Each module exposes a Protocol-style interface, a real implementation that
talks to the live site, and a fake implementation that returns canned data.
Scripts default to the real implementation; tests inject the fake one.
"""
