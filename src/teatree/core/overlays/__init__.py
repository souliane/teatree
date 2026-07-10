"""Core-side overlay seams that register into the platform config layer.

Holds the ``teatree.core`` (domain) halves of inverted dependencies whose
registration seam lives below in ``teatree.config`` (platform): the resolver
cannot import the overlay object, so the overlay side registers a provider at
import time. Currently the overlay-code-default provider (#36).
"""
