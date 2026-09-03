"""Generated recordings a literal token scan must not read as a source reference.

``dev/.test_durations`` is pytest-split's timing cassette, written by the
``refresh-durations`` job. It records the node id of every test that ran, so it
necessarily spells every token a conformance test scans for — including the ones
those very tests assert are retired. A refresh therefore reds the detectors
against their own recording (#4664).

Exactly this one machine-generated file is exempt. The rest of ``dev/`` stays
scanned: a detector that stops reading a directory because one file in it is
noisy is a worse detector.
"""

#: Repo-relative posix path of the cassette, as the scans spell their paths.
DURATIONS_CASSETTE = "dev/.test_durations"
