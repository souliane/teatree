"""First-party admin dashboard app served by the ``t3 admin`` gunicorn process (#3162).

A thin, ticket-centric operations surface over the existing read-only selectors,
health readers, and loop-control managers — server-rendered Django templates plus
vendored htmx polling, no SPA and no build step. Mounted at ``/dash/`` behind the
same loopback bind + SSH tunnel + auto-login boundary as the Django admin; it owns
no models and adds no second source of truth for loop or ticket state.
"""
