"""matchsim - synthetic, football-realistic match event generator.

No real match data is used: clubs, players, venues and events are all generated.
"""

from .engine import simulate_match
from .schema import SCHEMA_VERSION

__all__ = ["simulate_match", "SCHEMA_VERSION"]
