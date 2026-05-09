"""
USDA FDC API client — Phase 2 stub.
When implemented, this will fetch regional price history for ingredients
using the USDA_API_KEY and expose it to the scoring engine.
"""
from typing import List, Dict, Any, Optional


async def fetch_price_history(ingredient_name: str) -> List[Dict[str, Any]]:
    """Returns historical price data for an ingredient. Phase 2 implementation."""
    return []


async def get_regional_average(ingredient_name: str, region: str = "national") -> Optional[float]:
    """Returns the USDA regional average price per lb. Phase 2 implementation."""
    return None
