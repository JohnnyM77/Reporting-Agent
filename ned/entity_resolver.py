#!/usr/bin/env python3
# ned/entity_resolver.py
#
# Entity resolution module for disambiguating company mentions in news.
# Loads company entity configurations and provides matching/filtering logic.

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

import yaml


def load_company_entities(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """
    Load company entity configurations from YAML file.
    
    Args:
        path: Path to company_entities.yaml. If None, uses default location.
        
    Returns:
        Dict mapping ticker symbol to entity configuration.
    """
    if path is None:
        path = Path(__file__).parent / "company_entities.yaml"
    
    try:
        with open(path) as f:
            entities = yaml.safe_load(f) or {}
        return entities
    except Exception as exc:
        print(f"[ned/entity_resolver] Failed to load entities from {path}: {exc}")
        return {}


def build_google_news_query(entity: dict[str, Any]) -> str:
    """
    Build a Google News search query for a company entity.
    
    Combines aliases with optional context terms to improve precision.
    
    Args:
        entity: Company entity configuration dict
        
    Returns:
        URL-encoded query string suitable for Google News RSS
        
    Example:
        For ABB: ("Aussie Broadband" OR "Aussie Broadband Limited") (ASX OR Australia)
    """
    aliases = entity.get("aliases", [])
    optional_terms = entity.get("optional_terms", [])
    
    if not aliases:
        # Fallback to company name if no aliases defined
        company_name = entity.get("company_name", "")
        if company_name:
            aliases = [company_name]
    
    # Build alias portion with OR
    alias_parts = [f'"{alias}"' for alias in aliases]
    if len(alias_parts) > 1:
        alias_query = f"({' OR '.join(alias_parts)})"
    elif alias_parts:
        alias_query = alias_parts[0]
    else:
        return ""
    
    # Add optional context terms (helps with precision)
    if optional_terms:
        # Use a subset of most relevant optional terms to avoid overly restrictive queries
        # Prioritize exchange and country terms
        priority_terms = []
        for term in optional_terms[:3]:  # Limit to first 3 optional terms
            priority_terms.append(term)
        
        if priority_terms:
            context_query = f"({' OR '.join(priority_terms)})"
            full_query = f"{alias_query} {context_query}"
        else:
            full_query = alias_query
    else:
        full_query = alias_query
    
    return urllib.parse.quote_plus(full_query)


def matches_entity(text: str, entity: dict[str, Any]) -> tuple[bool, str]:
    """
    Check if a news item text matches a company entity.
    
    Uses multi-stage matching logic:
    1. Reject if any exclude_terms are present
    2. Accept if any alias phrase is present
    3. Otherwise require at least one required_term
    4. If optional_terms exist, require at least one of those too
    
    Args:
        text: Combined title + description text (should be lowercase)
        entity: Company entity configuration dict
        
    Returns:
        Tuple of (matches: bool, reason: str)
        reason describes why it matched or didn't match
    """
    # Normalize text to lowercase for case-insensitive matching
    text_lower = text.lower()
    
    # Stage 1: Check exclude terms (immediate rejection)
    exclude_terms = entity.get("exclude_terms", [])
    for term in exclude_terms:
        if term.lower() in text_lower:
            return False, f"excluded by term: {term}"
    
    # Stage 2: Check aliases (immediate acceptance)
    aliases = entity.get("aliases", [])
    for alias in aliases:
        # Use word boundary matching for aliases to avoid partial matches
        pattern = rf"\b{re.escape(alias.lower())}\b"
        if re.search(pattern, text_lower):
            return True, f"matched alias: {alias}"
    
    # Stage 3: Check required terms
    required_terms = entity.get("required_terms", [])
    required_match = None
    
    if required_terms:
        for term in required_terms:
            if term.lower() in text_lower:
                required_match = term
                break
        
        if not required_match:
            return False, "no required term found"
    
    # Stage 4: Check optional terms (if they exist, need at least one)
    optional_terms = entity.get("optional_terms", [])
    
    if optional_terms:
        optional_match = None
        for term in optional_terms:
            if term.lower() in text_lower:
                optional_match = term
                break
        
        if not optional_match:
            return False, "required term found but no optional term"
        
        reason = f"matched required: {required_match}, optional: {optional_match}"
        return True, reason
    else:
        # No optional terms required, just needed the required term
        if required_match:
            return True, f"matched required: {required_match}"
    
    # If we got here with no required terms, accept (edge case)
    return True, "accepted (no specific requirements)"


def get_yahoo_symbol(ticker: str, entity: dict[str, Any] | None = None) -> str:
    """
    Get the Yahoo Finance symbol for a ticker.
    
    Args:
        ticker: The ticker symbol (e.g., "ABB", "RR.")
        entity: Optional entity config dict. If provided, uses yahoo_symbol field.
        
    Returns:
        Yahoo Finance symbol (e.g., "ABB.AX", "RR.L")
    """
    if entity and "yahoo_symbol" in entity:
        return entity["yahoo_symbol"]
    
    # Fallback logic for tickers without explicit entity config
    if ticker == "RR.":
        return "RR.L"
    
    # Default: ASX ticker
    return f"{ticker}.AX"
