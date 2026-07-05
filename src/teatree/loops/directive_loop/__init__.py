"""The directive-driven self-modification front-end (north-star PR-6).

Turns a plain-language directive about teatree's own behavior into a ratified,
typed design contract: capture the words verbatim (``Directive``), interpret them
into a :class:`~teatree.core.models.mechanism_sketch.MechanismSketch` via a headless
read-only pass (:mod:`interpret`), and hold the design at ONE human checkpoint
before any code exists (:mod:`ratify`). Everything downstream — implement, configure,
verify — is a later PR's; this package carries the intake arc through ``ADMITTED``.

The safety model is the outer loop's, carried whole: interpretation is headless,
ratification is human, and ``Directive.admit`` RAISES without a consumed ratify
question — there is no auto-admit path. The whole front-end is inert at default
config (``directive_loop_enabled`` DARK; the router is parity-off), so capture
happens only via the explicit CLI until an overlay opts in.
"""
