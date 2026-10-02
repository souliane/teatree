"""Deterministic existence checks for cited external sources (PR-15, M5).

Intake-source verification: before teatree records a scanned-news candidate (or
any other cited URL), confirm the URL actually resolves — a fabricated or 404
citation is dropped, a genuine transport failure is surfaced distinctly rather
than silently dropped. The single public surface is :mod:`.url_check`.
"""
