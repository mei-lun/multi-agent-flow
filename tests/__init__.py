"""Project test package.

Keeping the test tree as an explicit package makes mypy resolve shared
fixtures consistently as ``tests.fixtures`` instead of discovering the same
files a second time as top-level ``fixtures`` modules.
"""
