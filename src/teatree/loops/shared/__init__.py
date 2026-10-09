"""Pure rules shared across loops — extracted so one implementation serves many.

The outer loop and the directive loop share the signal guards, the score
read, and the anti-Goodhart :func:`~teatree.loops.shared.regression.no_collateral_regression`
fold, so both gate and measure a change the same way.
"""
