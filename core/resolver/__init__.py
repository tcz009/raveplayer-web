# -*- coding: utf-8 -*-
"""Pluggable direct-link resolvers.

Each provider lives in its own submodule (for example :mod:`core.resolver.mega`)
and exposes a small ``resolve(link)`` contract.  This package intentionally
contains no imports so the parent service can add or replace adapters without
creating import cycles.
"""
