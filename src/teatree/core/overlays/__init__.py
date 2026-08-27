"""Core-side overlay seams the loader is over its module-health cap to hold.

``overlay_code_defaults_provider`` is the ``teatree.core`` (domain) half of an
inverted dependency whose registration seam lives below in ``teatree.config``
(platform): the resolver cannot import the overlay object, so the overlay side
registers a provider at import time (#36). ``overlay_namespace`` is a pure
matcher — it takes the overlay registry injected, so it carries no edge back to
the loader at all.
"""
