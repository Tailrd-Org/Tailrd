"""The scorer (SPEC §5, §6, §13, §15, §17): pure functions only.

No I/O, no clock, no config loading (an `EngineConfig` arrives as an argument), no randomness.
Imports are limited to the standard library, pydantic and `matcher.{models, errors, config,
graph}` (SPEC §1; DECISIONS D-02).
"""
