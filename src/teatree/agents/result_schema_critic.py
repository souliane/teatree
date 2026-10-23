"""Structured output property for one critic verdict."""

CRITIC_VERDICT_SCHEMA_PROPERTY = {
    "type": "object",
    "description": "The autonomous user-proxy critic's typed verdict, recorded server-side (SELFCATCH-5).",
    "properties": {
        "grader_identity": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail", "instrumentation_gap", "unverified"]},
                    "citation": {"type": "string"},
                    "verified": {"type": "boolean"},
                    "points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["blocking", "nit"]},
                                "severity": {"type": "string"},
                                "summary": {"type": "string"},
                                "citation": {"type": "string"},
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                            },
                        },
                    },
                    "resolutions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "finding_id": {"type": "string"},
                                "status": {"type": "string", "enum": ["fixed", "not_fixed"]},
                                "citation": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}
