"""
Secret scrubbing utilities for sanitizing exception messages and logs.

Prevents API keys, tokens, and other secrets from leaking into logs, dossiers,
state.json, or LLM prompts.
"""
import os
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


def safe_err(e: Exception) -> str:
    """Return a safe error message with secrets redacted.
    
    Redacts:
    - URL query strings containing api-key, apikey, or token parameters
    - Bare path+query forms like `/?api-key=SECRET` (from requests ConnectionError)
    - SOLANA_RPC_URL value, path, and host-relative forms
    - Any api-key/apikey/token/access-token parameter anywhere in the message
    
    Returns: "{ExceptionType}: {redacted_message}"
    
    If str(e) raises, returns "{ExceptionType}: <unprintable error>"
    
    Examples:
        >>> e = Exception("Max retries exceeded with url: /?api-key=SECRET123")
        >>> safe_err(e)
        'Exception: Max retries exceeded with url: /?api-key=REDACTED'
    """
    try:
        exc_type = type(e).__name__
        msg = str(e)
        
        # Get all *_RPC_URL environment variables (bug #11)
        rpc_urls = []
        for key, value in os.environ.items():
            if key.endswith("_RPC_URL") and value:
                rpc_urls.append(value)
        
        # Redact all RPC URLs
        for rpc_url in rpc_urls:
            if rpc_url in msg:
                redacted = _redact_url(rpc_url)
                msg = msg.replace(rpc_url, redacted)
            
            # Also redact path+query forms
            try:
                parsed = urlparse(rpc_url)
                if parsed.path or parsed.query:
                    path_query = parsed.path if parsed.path else "/"
                    if parsed.query:
                        path_query += f"?{parsed.query}"
                    if path_query in msg:
                        redacted_pq = _redact_url(f"https://dummy{path_query}").replace("https://dummy", "")
                        msg = msg.replace(path_query, redacted_pq)
            except Exception:
                pass
        
        # Redact query parameters in full URLs (api-key, apikey, token)
        url_pattern = r'(https?://[^\s\'"]+)'
        
        def redact_match(match):
            url = match.group(1)
            return _redact_url(url)
        
        msg = re.sub(url_pattern, redact_match, msg)
        
        # Final catch-all: redact any key=value patterns for sensitive parameters
        # Matches: api-key, api_key, apikey, token, access-token, access_token
        # Case-insensitive, captures parameter name to preserve it
        # Pattern: (api[-_]?key|apikey|token|access[-_]?token)=[^&\s'")]+
        secret_pattern = r'(api[-_]?key|apikey|token|access[-_]?token)=([^&\s\'")]+)'
        
        def redact_param(match):
            param_name = match.group(1)
            return f"{param_name}=REDACTED"
        
        msg = re.sub(secret_pattern, redact_param, msg, flags=re.IGNORECASE)

        # Redact raw env secret values (header-auth keys never appear as query params).
        for env_name in (
            "COINGECKO_API_KEY", "DESK_SECRET", "TYPESAFE_API_KEY",
            "COINALYZE_API_KEY", "FOMO_BEARER",
        ):
            val = os.environ.get(env_name)
            if val and len(val) >= 8 and val in msg:
                msg = msg.replace(val, "REDACTED")
        
        return f"{exc_type}: {msg}"
    except Exception:
        # If anything fails (including str(e)), return safe fallback
        try:
            exc_type = type(e).__name__
        except Exception:
            exc_type = "Exception"
        return f"{exc_type}: <unprintable error>"


def _redact_url(url: str) -> str:
    """Redact sensitive query parameters from a URL.
    
    Redacts values for: api-key, apikey, token (case-insensitive)
    """
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        redacted_params = {}
        
        for key, values in query_params.items():
            key_lower = key.lower()
            if any(secret in key_lower for secret in ['api-key', 'apikey', 'token']):
                # Redact all values for this parameter
                redacted_params[key] = ['REDACTED'] * len(values)
            else:
                redacted_params[key] = values
        
        # Rebuild query string
        redacted_query = urlencode(redacted_params, doseq=True)
        
        # Rebuild URL
        redacted_parsed = parsed._replace(query=redacted_query)
        return urlunparse(redacted_parsed)
    except Exception:
        # If URL parsing fails, return as-is rather than crash
        return url
