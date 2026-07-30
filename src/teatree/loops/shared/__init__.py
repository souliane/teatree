"""Pure rules shared across loops — extracted so one implementation serves many.

The first tenant is :func:`~teatree.loops.shared.regression.no_collateral_regression`,
extracted from the outer loop's ``decide.py`` so the outer loop AND the future
directive loop's VERIFYING step read the SAME anti-Goodhart fold, never two copies.
"""
